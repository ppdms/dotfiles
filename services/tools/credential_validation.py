"""Validate selected consumer inputs without exposing their contents in diagnostics."""

from __future__ import annotations

import base64
import binascii
import configparser
import ctypes
import datetime
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

FIELDS = {
    "backrest": {"restic-password": "restic/restic-password", "rclone.conf": "rclone/rclone.conf"},
    "authelia": {name: name for name in ("jwt-secret", "session-secret", "storage-encryption-key", "users.yml", "oidc-issuer.pem")},
    "authelia-oidc": {name: name for name in ("forgejo", "immich", "jellyfin", "termix", "kavita")},
    "nginx": {name: name for name in ("tls.crt", "tls.key", "tls-public.crt", "tls-public.key", "omp-web-token.conf")},
    "alloy": {name: "env/" + name for name in ("GRAFANA_CLOUD_LOKI_URL", "GRAFANA_CLOUD_LOKI_USER", "GRAFANA_CLOUD_API_KEY", "GRAFANA_CLOUD_PROM_URL", "GRAFANA_CLOUD_PROM_USER", "GRAFANA_CLOUD_READ_TOKEN")},
    "immich": {name: name for name in ("postgres-password", "db-username", "db-password", "db-name")},
    "cloudflared": {"tunnel-token": "tunnel-token"},
    "opencloud": {"opencloud.yaml": "opencloud.yaml"},
    "termix": {"oidc-client-secret": "oidc-client-secret"},
    "transmission": {"wireguard-private-key": "wireguard-private-key"},
    "kavita": {"oidc-client-secret": "oidc-client-secret"},
    "tree-eclass": {"provider-keys": "tree-eclass/provider-keys"},
    "homeassistant": {"secrets.yaml": "secrets.yaml"},
    "esphome": {"dashboard-password": "dashboard-password"},
    "image-updates": {"github-token": "github-token"},
    "vpn-ddns": {"api-token": "api-token"},
    # Operator-only: no service principal lists it, so the broker never serves it
    # to a service and boot-time initialization never asks for a seed.
    "tern": {"stencil-refresh-token": "stencil-refresh-token"},
}
MUTABLE = {"backrest": "rclone.conf", "authelia": "users.yml"}
# Fields a component works without. An empty optional field means "unset", so
# adding one never blocks starting the consumers of an already-provisioned record.
OPTIONAL = {"nginx": frozenset({"omp-web-token.conf"})}

WEB_TOKEN_SNIPPET = re.compile(r'set \$omp_web_token "([A-Za-z0-9_-]{16,128})";\n?')
STENCIL_REFRESH_TOKEN = re.compile(r"[\x21-\x7e]{16,8192}")


def required_fields(component: str) -> list[str]:
    return [name for name in FIELDS[component] if name not in OPTIONAL.get(component, ())]


def web_token_snippet(token: str) -> str:
    """The nginx include that hands Tern's /wire token to the omp vhosts."""
    snippet = f'set $omp_web_token "{token}";\n'
    if WEB_TOKEN_SNIPPET.fullmatch(snippet) is None:
        raise ValueError("omp-web-token.conf: token must be 16-128 characters of A-Z a-z 0-9 _ -")
    return snippet


def web_token_from_snippet(snippet: str) -> str:
    match = WEB_TOKEN_SNIPPET.fullmatch(snippet)
    if match is None:
        raise ValueError('omp-web-token.conf: must be exactly: set $omp_web_token "<token>";')
    return match[1]


def _field_shape(component: str, fields: dict[str, str], complete: bool) -> None:
    if not isinstance(component, str) or component not in FIELDS:
        raise ValueError("credentials: unknown component")
    if not isinstance(fields, dict):
        raise ValueError("files: must be an object")
    if any(not isinstance(name, str) or name not in FIELDS[component] for name in fields):
        raise ValueError("files: contains an unknown field")
    if complete:
        for name in required_fields(component):
            if name not in fields:
                raise ValueError(f"{name}: required field is missing")
    for name, value in fields.items():
        if not isinstance(value, str):
            raise ValueError(f"{name}: must be text")
        if "\x00" in value:
            raise ValueError(f"{name}: must not contain NUL")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(f"{name}: must be valid UTF-8 text") from None


def _unique_json(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("credentials: duplicate JSON key")
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> None:
    raise ValueError("credentials: JSON constants must be finite")


def parse_payload(component: str, data: bytes, complete: bool = True) -> dict[str, str]:
    """Read the version-1, single-service env/files credential contract."""
    if not isinstance(component, str) or component not in FIELDS:
        raise ValueError("credentials: unknown component")
    if not isinstance(data, bytes):
        raise ValueError("credentials: payload must be UTF-8 bytes")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_json,
                             parse_constant=_invalid_json_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("credentials: invalid UTF-8 JSON or duplicate JSON key") from None
    if not isinstance(payload, dict) or set(payload) != {"version", "services"}:
        raise ValueError("credentials: expected version and services only")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ValueError("version: must be integer 1")
    services = payload["services"]
    if not isinstance(services, dict) or set(services) != {component}:
        raise ValueError("services: must contain only the selected component")
    service = services[component]
    if not isinstance(service, dict) or set(service) != {"env", "files"}:
        raise ValueError("services: selected component must contain env and files only")
    if not isinstance(service["env"], dict) or service["env"]:
        raise ValueError("env: must be an empty object; secret environment variables are unsupported")
    fields = service["files"]
    validate_fields(component, fields, complete=complete)
    return fields


def payload_bytes(component: str, fields: dict[str, str]) -> bytes:
    """Serialize deterministically, including incomplete editor templates.

    Call validate_fields or parse_payload before committing a credential. This
    serializer intentionally does not reject empty values in editor templates.
    """
    _field_shape(component, fields, complete=False)
    payload = {"version": 1, "services": {component: {"env": {}, "files": fields}}}
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


class _UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        # Flatten merges first so collisions through YAML aliases cannot hide a
        # duplicate key. All errors are replaced before leaving the validator.
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError("duplicate YAML key")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _base64_part(value: str, adapted: bool = False) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9./+]*" if adapted else r"[A-Za-z0-9/+]*", value):
        raise ValueError("invalid hash encoding")
    if adapted:
        value = value.replace(".", "+")
    return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)


def _hash_parameters(value: str, allowed: set[str]) -> dict[str, int]:
    parameters = {}
    for part in value.split(","):
        match = re.fullmatch(r"([a-z]+)=([0-9]+)", part)
        if match is None or match[1] not in allowed or match[1] in parameters:
            raise ValueError("invalid hash parameters")
        number = int(match[2])
        if number > 2**32 - 1:
            raise ValueError("invalid hash parameters")
        parameters[match[1]] = number
    return parameters


def _password_hash(value: str, field: str = "users.yml") -> None:
    """Check the formats loaded by go-crypt v0.14.15's default decoder.

    Do not hash candidate passwords, enforce a new strength policy, or require a
    password login. Passkey users retain their existing file-backend hash.
    """
    try:
        if value.startswith("{CRYPT}"):
            value = value[7:]
        if value.startswith("{ARGON2}"):
            value = value[8:]
        ldap = re.fullmatch(r"\{(PBKDF2(?:-SHA[0-9]+)?)\}([0-9]+\$.*)", value)
        if ldap:
            value = "$" + ldap[1].lower() + "$" + ldap[2]
        parts = value.split("$")
        if len(parts) < 4 or parts[0] != "":
            raise ValueError("invalid hash")
        algorithm = parts[1]
        if algorithm in {"argon2id", "argon2i", "argon2d"}:
            if len(parts) != 6 or parts[2] != "v=19":
                raise ValueError("invalid hash")
            params = _hash_parameters(parts[3], {"m", "t", "p", "k"})
            if params.get("p", 4) > 255:
                raise ValueError("invalid hash")
            _base64_part(parts[4])
            if not _base64_part(parts[5]):
                raise ValueError("invalid hash")
        elif algorithm in {"2", "2a", "2b", "2x", "2y"}:
            if not re.fullmatch(r"\$2[abxy]?\$[0-9]{1,2}\$[./A-Za-z0-9]{53}", value):
                raise ValueError("invalid hash")
            if not 4 <= int(parts[2]) <= 31:
                raise ValueError("invalid hash")
        elif algorithm == "bcrypt-sha256":
            if len(parts) != 5:
                raise ValueError("invalid hash")
            options = {}
            for part in parts[2].split(","):
                name, separator, item = part.partition("=")
                if not separator or name not in {"v", "t", "r"} or name in options:
                    raise ValueError("invalid hash")
                options[name] = item
            if not re.fullmatch(r"[0-9]+", options.get("v", "")) or options.get("t") not in {"2a", "2b", "2x", "2y"}:
                raise ValueError("invalid hash")
            if not re.fullmatch(r"[0-9]+", options.get("r", "")) or not 4 <= int(options["r"]) <= 31:
                raise ValueError("invalid hash")
            if not re.fullmatch(r"[./A-Za-z0-9]{22}", parts[3]) or not re.fullmatch(r"[./A-Za-z0-9]{31}", parts[4]):
                raise ValueError("invalid hash")
        elif algorithm in {"pbkdf2", "pbkdf2-sha1", "pbkdf2-sha224", "pbkdf2-sha256", "pbkdf2-sha384", "pbkdf2-sha512"}:
            if len(parts) != 5 or not re.fullmatch(r"[0-9]+", parts[2]) or not 1 <= int(parts[2]) <= 2**63 - 1:
                raise ValueError("invalid hash")
            _base64_part(parts[3], adapted=True)
            if not _base64_part(parts[4], adapted=True):
                raise ValueError("invalid hash")
        elif algorithm == "scrypt":
            if len(parts) != 5:
                raise ValueError("invalid hash")
            params = _hash_parameters(parts[2], {"ln", "r", "p"})
            if not 1 <= params.get("ln", 16) <= 63 or params.get("r", 8) < 1 or params.get("p", 1) < 1:
                raise ValueError("invalid hash")
            _base64_part(parts[3])
            if not _base64_part(parts[4]):
                raise ValueError("invalid hash")
        elif algorithm == "y":
            if len(parts) != 5 or not all(re.fullmatch(r"[./A-Za-z0-9]+", part) for part in parts[2:]):
                raise ValueError("invalid hash")
            if not re.fullmatch(r"j[./A-Za-z0-9]{2}", parts[2]):
                raise ValueError("invalid hash")
            alphabet = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            for encoded in parts[3:]:
                # yescrypt uses little-endian crypt base64 with zero unused bits.
                if len(encoded) % 4 == 1:
                    raise ValueError("invalid hash")
                remaining = len(encoded) * 6 % 8
                if remaining and alphabet.index(encoded[-1]) >> (6 - remaining):
                    raise ValueError("invalid hash")
        elif algorithm in {"5", "6"}:
            if len(parts) == 5:
                _hash_parameters(parts[2], {"rounds"})
            elif len(parts) != 4:
                raise ValueError("invalid hash")
            if not re.fullmatch(r"[./A-Za-z0-9]{0,16}", parts[-2]):
                raise ValueError("invalid hash")
            if not re.fullmatch(r"[./A-Za-z0-9]{" + ("43" if algorithm == "5" else "86") + "}", parts[-1]):
                raise ValueError("invalid hash")
        else:
            raise ValueError("unsupported hash")
    except (ValueError, binascii.Error, OverflowError):
        raise ValueError(f"{field}: password must be a supported encoded password hash") from None


def _yaml_text(value: object, ancestors: set[int]) -> None:
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("NUL in YAML")
        value.encode("utf-8")
    elif isinstance(value, (dict, list)):
        identity = id(value)
        if identity in ancestors:
            raise ValueError("recursive YAML alias")
        ancestors.add(identity)
        if isinstance(value, dict):
            for key, item in value.items():
                _yaml_text(key, ancestors)
                _yaml_text(item, ancestors)
        else:
            for item in value:
                _yaml_text(item, ancestors)
        ancestors.remove(identity)


def _users(value: str) -> None:
    try:
        document = yaml.load(value, Loader=_UniqueSafeLoader)
        _yaml_text(document, set())
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        raise ValueError("users.yml: invalid YAML, duplicate key, NUL, or recursive alias") from None
    if not isinstance(document, dict) or set(document) != {"users"}:
        raise ValueError("users.yml: expected a users mapping")
    users = document["users"]
    if not isinstance(users, dict) or not users:
        raise ValueError("users.yml: users must be a nonempty mapping")
    text_fields = {
        "password", "displayname", "email", "given_name", "middle_name", "family_name",
        "nickname", "gender", "birthdate", "website", "profile", "picture", "zoneinfo",
        "locale", "phone_number", "phone_extension",
    }
    for username, user in users.items():
        if not isinstance(username, str) or not username or not isinstance(user, dict):
            raise ValueError("users.yml: each username must be nonempty text with a user mapping")
        if any(not isinstance(key, str) for key in user):
            raise ValueError("users.yml: user attribute names must be text")
        for name in text_fields:
            if name in user and not isinstance(user[name], str):
                raise ValueError(f"users.yml: {name} must be text")
        for name in ("displayname", "password"):
            if not user.get(name):
                raise ValueError(f"users.yml: {name} is required by the file backend")
        _password_hash(user["password"])
        if "groups" in user and user["groups"] is not None and (not isinstance(user["groups"], list) or any(not isinstance(group, str) for group in user["groups"])):
            raise ValueError("users.yml: groups must be a list of text values")
        if "disabled" in user and type(user["disabled"]) is not bool:
            raise ValueError("users.yml: disabled must be a boolean")
        if "address" in user and user["address"] is not None:
            address = user["address"]
            if not isinstance(address, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in address.items()):
                raise ValueError("users.yml: address must be a mapping of text attributes")
        # The deployed configuration declares no extra_attributes. Upstream's
        # ValidateExtra refuses every nonempty extra attribute in that case.
        if "extra" in user and user["extra"] not in (None, {}):
            raise ValueError("users.yml: extra attributes are not configured for this file backend")


def _openssl(arguments: list[str], field: str, reason: str, data: bytes | None = None) -> bytes:
    executable = os.environ.get("HOMELAB_OPENSSL")
    if not executable or not os.path.isabs(executable):
        raise ValueError("tls: HOMELAB_OPENSSL must name the packaged executable")
    try:
        result = subprocess.run([executable, *arguments], input=data,
                                stdin=subprocess.DEVNULL if data is None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env={**os.environ, "LC_ALL": "C"}, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError(f"{field}: OpenSSL validation could not run") from None
    if result.returncode:
        raise ValueError(f"{field}: {reason}")
    return result.stdout


def _tls_dates(openssl_field: str, directory, complete: bool) -> None:
    certificate = str(directory / openssl_field)
    dates = _openssl(["x509", "-in", certificate, "-noout", "-dates"], openssl_field, "certificate validity could not be read")
    try:
        lines = dates.decode("ascii").splitlines()
        if len(lines) != 2 or not lines[0].startswith("notBefore=") or not lines[1].startswith("notAfter="):
            raise ValueError("invalid certificate dates")
        parsed = []
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        for line in lines:
            match = re.fullmatch(r"[a-zA-Z]+=(\w{3}) +([0-9]{1,2}) ([0-9]{2}):([0-9]{2}):([0-9]{2}) ([0-9]{4}) GMT", line)
            if match is None:
                raise ValueError("invalid certificate dates")
            parsed.append(datetime.datetime(int(match[6]), months.index(match[1]) + 1, int(match[2]),
                                            int(match[3]), int(match[4]), int(match[5]),
                                            tzinfo=datetime.timezone.utc))
        start, end = parsed
    except (ValueError, UnicodeDecodeError):
        raise ValueError(f"{openssl_field}: certificate validity could not be read") from None
    now = datetime.datetime.now(datetime.timezone.utc)
    # An expired record must still load for renewal. Complete validation is
    # required before committing or delivering it.
    if complete and now < start:
        raise ValueError(f"{openssl_field}: certificate is not yet valid")
    if complete and now >= end:
        raise ValueError(f"{openssl_field}: certificate has expired")


def _tls_pair(fields: dict[str, str], directory, cert_field: str, key_field: str, hostnames: tuple[str, ...], complete: bool) -> bytes | None:
    cert_public = None
    if cert_field in fields:
        certificate = str(directory / cert_field)
        cert_public_pem = _openssl(["x509", "-in", certificate, "-noout", "-pubkey"], cert_field, "must contain a parseable PEM certificate")
        cert_public = _openssl(["pkey", "-pubin", "-outform", "DER"], cert_field, "certificate public key is invalid", cert_public_pem)
        alternatives = _openssl(["x509", "-in", certificate, "-noout", "-ext", "subjectAltName"], cert_field, "certificate subject alternatives could not be read")
        if not re.search(rb"(?:^|[\s,])DNS:", alternatives):
            raise ValueError(f"{cert_field}: certificate must declare DNS subject alternative names")
        _tls_dates(cert_field, directory, complete)
        for hostname in hostnames:
            match = _openssl(["x509", "-in", certificate, "-noout", "-checkhost", hostname], cert_field, "certificate hostname check failed")
            # x509 -checkhost can return zero even for a mismatch.
            if match.strip() != f"Hostname {hostname} does match certificate".encode("ascii"):
                raise ValueError(f"{cert_field}: certificate must cover {hostname}")
    if key_field in fields:
        if re.search(r"(?m)^(?:-----BEGIN ENCRYPTED PRIVATE KEY-----|Proc-Type: *4,ENCRYPTED)\r?$", fields[key_field]):
            raise ValueError(f"{key_field}: nginx requires an unencrypted private key")
        key = str(directory / key_field)
        _openssl(["pkey", "-in", key, "-passin", "pass:", "-check", "-noout"], key_field, "must contain a valid unencrypted PEM private key")
        key_public = _openssl(["pkey", "-in", key, "-passin", "pass:", "-pubout", "-outform", "DER"], key_field, "private key public key could not be read")
        if cert_public is not None and cert_public != key_public:
            raise ValueError(f"{key_field}: private key does not match {cert_field}")
    return cert_public

def _tls(fields: dict[str, str], complete: bool) -> None:
    if not fields:
        return
    # Linux statfs has an architecture-native long f_type at offset zero. The
    # oversized buffer accommodates the remaining opaque platform structure.
    root = "/dev/shm"
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        status = ctypes.create_string_buffer(256)
        if libc.statfs(os.fsencode(root), ctypes.byref(status)) != 0 or ctypes.c_long.from_buffer(status).value != 0x01021994:
            raise ValueError("tls: a tmpfs temporary directory is required")
        with tempfile.TemporaryDirectory(prefix="homelab-tls-", dir=root) as temporary:
            directory = Path(temporary)
            for name, value in fields.items():
                path = directory / name
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(value.encode("utf-8"))
            if complete:
                lan_domain = os.environ.get("HOMELAB_LAN_DOMAIN")
                public_domain = os.environ.get("HOMELAB_PUBLIC_DOMAIN")
                if not lan_domain or not public_domain:
                    raise ValueError("tls: HOMELAB_LAN_DOMAIN and HOMELAB_PUBLIC_DOMAIN are required")
                _tls_pair(fields, directory, "tls.crt", "tls.key", tuple(f"{host}.{lan_domain}" for host in ("backup", "auth")), complete)
                _tls_pair(fields, directory, "tls-public.crt", "tls-public.key", tuple(f"{host}.{public_domain}" for host in ("auth", "photos", "notes")), complete)
            else:
                # An expired record must still load for renewal. Full date +
                # hostname checks run only for complete validation.
                for cert_field, key_field in (("tls.crt", "tls.key"), ("tls-public.crt", "tls-public.key")):
                    if cert_field in fields or key_field in fields:
                        _tls_pair(fields, directory, cert_field, key_field, (), complete)
    except OSError:
        raise ValueError("tls: private tmpfs validation files could not be created or removed") from None


def validate_fields(component: str, fields: dict[str, str], complete: bool = True) -> None:
    """Validate every present field, optionally requiring the complete selection."""
    _field_shape(component, fields, complete)
    for name, value in fields.items():
        if not value and name not in OPTIONAL.get(component, ()):
            raise ValueError(f"{name}: must not be empty")
    if component == "backrest" and "rclone.conf" in fields:
        parser = configparser.RawConfigParser(interpolation=None, strict=True)
        try:
            parser.read_string(fields["rclone.conf"])
        except configparser.Error:
            raise ValueError("rclone.conf: invalid or duplicate INI section/option") from None
        if not parser.has_section("GDrive") or parser.get("GDrive", "type", fallback="") != "drive":
            raise ValueError("rclone.conf: [GDrive] remote must have type=drive")
    elif component == "authelia":
        # v4.39.28 removes terminal newlines when loading *_FILE inputs. Check
        # that effective value without changing the stored bytes.
        for name in ("jwt-secret", "session-secret", "storage-encryption-key"):
            if name in fields:
                effective = fields[name].rstrip("\r\n")
                if not effective:
                    raise ValueError(f"{name}: must not be empty after file newline handling")
                # ValidateStorage requires len(key) >= 20 Go/UTF-8 bytes. The
                # reset-password JWT validator only requires a nonempty secret;
                # the deployed in-memory session backend has no length minimum.
                if name == "storage-encryption-key" and len(effective.encode("utf-8")) < 20:
                    raise ValueError("storage-encryption-key: must contain at least 20 UTF-8 bytes after file newline handling")
        if "users.yml" in fields:
            _users(fields["users.yml"])
        if "oidc-issuer.pem" in fields:
            pem = fields["oidc-issuer.pem"]
            if "PRIVATE KEY" not in pem or "-----BEGIN" not in pem:
                raise ValueError("oidc-issuer.pem: must be a PEM private key")
    elif component == "authelia-oidc":
        for name, value in fields.items():
            _password_hash(value, field=name)
    elif component == "nginx":
        if fields.get("omp-web-token.conf"):
            web_token_from_snippet(fields["omp-web-token.conf"])
        _tls({name: value for name, value in fields.items() if name != "omp-web-token.conf"}, complete)
    elif component == "alloy":
        for name in ("GRAFANA_CLOUD_LOKI_URL", "GRAFANA_CLOUD_PROM_URL"):
            if name in fields and not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::\d+)?(?:/\S*)?", fields[name].strip()):
                raise ValueError(f"{name}: must be an https URL")
    elif component == "immich":
        # Non-empty check above suffices; the server validates DB connectivity.
        pass
    elif component == "cloudflared":
        # Tunnel token is a Cloudflare-issued JWT; non-empty suffices here,
        # cloudflared validates it against the edge at startup.
        pass
    elif component == "opencloud" and "opencloud.yaml" in fields:
        try:
            configuration = yaml.load(fields["opencloud.yaml"], Loader=_UniqueSafeLoader)
        except (yaml.YAMLError, ValueError):
            raise ValueError("opencloud.yaml: invalid or duplicate YAML key") from None
        if not isinstance(configuration, dict):
            raise ValueError("opencloud.yaml: must be a configuration mapping")
        # Require the OpenCloud configuration's identity and authentication fields.
        # Diagnostics never include secret values.
        for name in ("system_user_id", "admin_user_id", "machine_auth_api_key", "system_user_api_key"):
            if not isinstance(configuration.get(name), str) or not configuration[name]:
                raise ValueError(f"opencloud.yaml: missing {name}")
        token_manager = configuration.get("token_manager")
        if not isinstance(token_manager, dict) or not isinstance(token_manager.get("jwt_secret"), str) or not token_manager["jwt_secret"]:
            raise ValueError("opencloud.yaml: missing token_manager.jwt_secret")
    elif component == "termix":
        # OIDC client secret is an Authelia-issued hex string; Termix sends it
        # at the token endpoint. Non-empty check above suffices; Authelia
        # validates it on token exchange.
        pass
    elif component == "transmission":
        # WireGuard private key is consumed by gluetun; the tunnel validates
        # it against Proton at startup. Non-empty check above suffices.
        pass
    elif component == "kavita":
        # OIDC client secret plaintext; Authelia receives its verifier from
        # the separate private authelia-oidc runtime credential bundle.
        # Authelia validates it on token exchange.
        pass
    elif component == "homeassistant":
        # HA secrets.yaml is YAML consumed by HA itself (!secret refs);
        # HA validates it at startup. Non-empty check above suffices.
        pass
    elif component == "esphome":
        # ESPHome dashboard password, sent as ESPHOME_PASSWORD env.
        # Non-empty check above suffices.
        pass
    elif component == "image-updates":
        token = fields.get("github-token")
        if token is not None and not re.fullmatch(r"[A-Za-z0-9._~+/\-]+=*", token):
            raise ValueError("github-token: must be a single-line ASCII bearer token")
    elif component == "vpn-ddns":
        token = fields.get("api-token")
        if token is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise ValueError("api-token: must be a single-line Cloudflare API token")
    elif component == "tern":
        # Stencil's refresh token is opaque and single-use (it rotates on every
        # refresh), so only its transport safety is checked here.
        token = fields.get("stencil-refresh-token")
        if token is not None and STENCIL_REFRESH_TOKEN.fullmatch(token) is None:
            raise ValueError("stencil-refresh-token: must be a single-line printable ASCII token")
