#!/usr/bin/env python3
"""Seal local metadata and construct the only directory approved as a Nix input."""

import argparse
import base64
import binascii
import configparser
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import uuid

def _find_schema():
    override = os.environ.get("PROFILE_SCHEMA_PATH")
    if override:
        return Path(override)
    here = Path(__file__).resolve().parent
    for base in (here, here.parent, here.parent.parent):
        candidate = base / "settings" / "profile-schema.json"
        if candidate.is_file():
            return candidate
    return here.parent / "settings" / "profile-schema.json"


SCHEMA_PATH = _find_schema()
MAX_BYTES = 16 * 1024 * 1024
CREDENTIAL_NAME = "nix-config-profile"
TPM_USER_HEADERS = {
    bytes.fromhex("ef4ac13679a9480ea7db68897f9f165d"),
    bytes.fromhex("adbc4ca3efb64201ba881b6f2e4095ea"),
}
PRIVATE_MARKERS = ("PRIVATE KEY", "AGE-SECRET-KEY-", "age-secret-key-")
FORBIDDEN_NAME = re.compile(r"api[_-]?key|password|access[_-]?token|private[_-]?key|auth[_-]?data", re.I)
ENC = re.compile(r"ENC\[AES256_GCM,data:([A-Za-z0-9+/=]+),iv:([A-Za-z0-9+/=]+),tag:([A-Za-z0-9+/=]+),type:(str|int|float|bool|bytes|comment)\]")


class ProfileError(Exception):
    pass


def fail(path, reason):
    # Never interpolate input values, unknown field names, or subprocess stderr.
    raise ProfileError(f"Invalid private profile at {path}: {reason}")


def parse_json(data, label):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProfileError(f"Invalid {label}: duplicate JSON fields")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: fail(label, "non-finite numbers are forbidden"))
    except (ValueError, UnicodeError, RecursionError):
        raise ProfileError(f"Invalid {label}: expected bounded UTF-8 JSON") from None


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def read_file(path, label, limit=MAX_BYTES):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ProfileError(f"Invalid {label}: expected a bounded regular file")
            data = source.read(limit + 1)
            if len(data) > limit:
                raise ProfileError(f"Invalid {label}: file is too large")
            return data
    except OSError:
        raise ProfileError(f"Missing or unsafe {label}") from None


def secure_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ProfileError("Refusing an unsafe private state directory")
    os.chmod(path, 0o700)


def atomic_file(path, data, exclusive=False):
    secure_directory(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ProfileError("Refusing an unsafe local output file")
    fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def command(argv, label, data=None, env=None):
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    except OSError:
        raise ProfileError(f"Unavailable command for {label}") from None
    def provide_input():
        try:
            process.stdin.write(data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()
    writer = None
    if data is not None:
        writer = threading.Thread(target=provide_input)
        writer.start()
    try:
        output = process.stdout.read(MAX_BYTES + 1)
        if len(output) > MAX_BYTES:
            process.kill()
        result = process.wait()
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        if writer is not None:
            writer.join()
    if result or len(output) > MAX_BYTES:
        raise ProfileError(f"Failed {label}; private command output omitted")
    return output


class Contract:
    def __init__(self):
        self.schema = parse_json(read_file(SCHEMA_PATH, "profile schema"), "profile schema")

    def resolve(self, rule):
        if "$ref" in rule:
            return self.schema["$defs"][rule["$ref"].removeprefix("#/$defs/")]
        return rule

    def validate(self, rule, value, path):
        rule = self.resolve(rule)
        types = rule.get("type")
        types = types if isinstance(types, list) else [types]
        checks = {"object": lambda: isinstance(value, dict),
                  "array": lambda: isinstance(value, list),
                  "string": lambda: isinstance(value, str),
                  "integer": lambda: type(value) is int,
                  "boolean": lambda: type(value) is bool,
                  "null": lambda: value is None}
        if not any(checks.get(kind, lambda: False)() for kind in types):
            fail(path, "unexpected value type")
        if "enum" in rule and value not in rule["enum"]:
            fail(path, "value is not an approved choice")
        if value is None:
            return None
        if isinstance(value, dict):
            properties = rule.get("properties", {})
            additional = rule.get("additionalProperties", False)
            if set(value) - set(properties) and additional is False:
                fail(path, "unrecognized fields")
            merged = dict(value)
            for name, raw_child in properties.items():
                child = self.resolve(raw_child)
                if name not in merged:
                    if "default" in child:
                        merged[name] = child["default"]
                    elif "x-default-from" in child and child["x-default-from"] in merged:
                        merged[name] = merged[child["x-default-from"]]
            if set(rule.get("required", [])) - set(merged):
                fail(path, "missing required fields")
            if len(value) < rule.get("minProperties", 0):
                fail(path, "too few entries")
            if "anyOf" in rule and not any(set(branch.get("required", [])) <= set(merged)
                                            for branch in rule["anyOf"]):
                fail(path, "missing required field combination")
            result = {}
            for name, child in merged.items():
                if FORBIDDEN_NAME.search(name):
                    fail(path, "credential-bearing field names are forbidden")
                if "propertyNames" in rule:
                    self.validate(rule["propertyNames"], name, path + ".<name>")
                child_path = path + ("." + name if name in properties else ".<entry>")
                result[name] = self.validate(properties.get(name, additional), child, child_path)
            return result
        if isinstance(value, list):
            if len(value) > rule.get("maxItems", 256):
                fail(path, "too many entries")
            return [self.validate(rule["items"], child, f"{path}[{index}]")
                    for index, child in enumerate(value)]
        if isinstance(value, str):
            if any(marker in value for marker in PRIVATE_MARKERS):
                fail(path, "private-key material is forbidden")
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                fail(path, "control characters are forbidden")
            if re.search(r"://[^/]*:[^/@]*@", value):
                fail(path, "inline URI credentials are forbidden")
            if not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", 8192):
                fail(path, "invalid string length")
            if "pattern" in rule and re.fullmatch(rule["pattern"], value) is None:
                fail(path, "invalid string format")
        elif type(value) is int:
            if ("minimum" in rule and value < rule["minimum"]) or ("maximum" in rule and value > rule["maximum"]):
                fail(path, "integer is outside the supported range")
        return value

    def definition(self, name, value):
        return self.validate(self.schema["$defs"][name], value, name)

    def project(self, source, platform):
        if not isinstance(source, dict) or not isinstance(source.get("profiles"), dict):
            fail("profiles", "expected a platform profile map")
        if platform not in source["profiles"]:
            fail("profiles", "requested platform is missing")
        selected = self.validate(self.schema["$defs"][platform], source["profiles"][platform], "profiles." + platform)
        # Deliberately never traverse an unselected profile, files, wireguard,
        # snapshots, or any other private state when constructing Nix input.
        return {"profiles": {platform: selected}}


def sops_environment(args):
    env = os.environ.copy()
    identity = args.identity or os.environ.get("NIX_CONFIG_INNER_IDENTITY")
    if identity is None:
        identity = str(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sops/age/outer-key.txt")
    if not Path(identity).is_file():
        raise ProfileError("Missing local SOPS identity")
    env["SOPS_AGE_KEY_FILE"] = str(identity)
    return env


def backend_path(args):
    return args.state_dir / ("profile.sops.yaml" if args.backend == "sops" else "profile.cred")


def require_tpm_credential(data):
    try:
        decoded = base64.b64decode(b"".join(data.split()), validate=True)
    except (ValueError, binascii.Error):
        raise ProfileError("Invalid TPM credential encoding") from None
    if decoded[:16] not in TPM_USER_HEADERS:
        raise ProfileError("Refusing a credential not bound to the TPM, host, and user")


def load_sealed(args):
    ciphertext = read_file(backend_path(args), "sealed profile")
    if args.backend == "sops":
        policy = args.state_dir / ".sops.yaml"
        read_file(policy, "local SOPS policy")
        envelope = parse_json(command(["sops", "--config", str(policy), "--decrypt", "--output-type", "json", str(backend_path(args))],
                                      "SOPS profile decryption", env=sops_environment(args)), "decrypted profile envelope")
        if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), str):
            raise ProfileError("Invalid sealed profile payload")
        return parse_json(envelope["payload"], "decrypted profile")
    require_tpm_credential(ciphertext)
    return parse_json(command(["systemd-creds", "--user", "--no-ask-password", "--name=" + CREDENTIAL_NAME,
                               "decrypt", str(backend_path(args)), "-"], "TPM profile decryption"), "decrypted profile")


def seal_bytes(args, source):
    if args.backend == "sops":
        policy = args.state_dir / ".sops.yaml"
        read_file(policy, "local SOPS policy")
        return command(["sops", "--encrypt", "--input-type", "json", "--output-type", "yaml",
                        "--filename-override", "profile.sops.yaml", "--config", str(policy), "/dev/stdin"],
                       "SOPS profile encryption", json_bytes({"payload": json_bytes(source).decode()}), sops_environment(args))
    # systemd-creds would silently create this key if absent. Require the
    # existing host identity instead; never run setup or rotate any key.
    try:
        info = Path("/var/lib/systemd/credential.secret").stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            raise OSError()
    except OSError:
        raise ProfileError("Existing systemd credential host key is required; refusing to generate one") from None
    encrypted = command(["systemd-creds", "--user", "--no-ask-password", "--name=" + CREDENTIAL_NAME,
                         "--with-key=host+tpm2", "encrypt", "-", "-"], "TPM profile encryption", json_bytes(source))
    require_tpm_credential(encrypted)
    return encrypted


def encrypted_value(value):
    if not isinstance(value, str):
        return False
    match = ENC.fullmatch(value)
    if match is None:
        return False
    try:
        data, iv, tag = (base64.b64decode(item, validate=True) for item in match.groups()[:3])
    except (ValueError, binascii.Error):
        return False
    return bool(data) and len(iv) in (12, 32) and len(tag) == 16


def age_ciphertext(value):
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r"\s*-----BEGIN AGE ENCRYPTED FILE-----\s+([A-Za-z0-9+/=\s]+)-----END AGE ENCRYPTED FILE-----\s*", value)
    if match is None:
        return False
    try:
        decoded = base64.b64decode("".join(match.group(1).split()), validate=True)
    except (ValueError, binascii.Error):
        return False
    return decoded.startswith(b"age-encryption.org/v1\n") and b"--- " in decoded


def encrypted_comment(value):
    # A scalar ciphertext masquerading as a comment is not a SOPS comment.
    value = value.strip()
    if not value:
        return
    if not encrypted_value(value) or not value.endswith(",type:comment]"):
        raise ValueError()


def yaml_ciphertext_comments(text, yaml):
    # The scanner knows quoted/plain scalars, tags and block-scalar content.
    # Comments occur only in the gaps between tokens, except for the header
    # comment of a block scalar, which PyYAML includes in that scalar's span.
    cursor = 0
    gaps = []
    for token in yaml.scan(text):
        start, end = token.start_mark.index, token.end_mark.index
        if start > cursor:
            gaps.append(text[cursor:start])
        if isinstance(token, yaml.tokens.ScalarToken) and token.style in ("|", ">"):
            gaps.append(text[start:text.find("\n", start) if "\n" in text[start:end] else end])
        cursor = max(cursor, end)
    gaps.append(text[cursor:])
    for gap in gaps:
        for line in gap.splitlines():
            if "#" in line:
                encrypted_comment(line.split("#", 1)[1])


def ini_ciphertext_comments(text):
    # SOPS uses go-ini's native #/; comments (including inline comments).
    # Its emitted quoting for values containing comment symbols is backtick
    # or triple-double-quote, not ordinary INI single/double quotes. Build a
    # validation-only view for ConfigParser; the original bytes are preserved.
    lines = iter(text.splitlines())
    result = []
    for line in lines:
        stripped = line.lstrip()
        if not stripped:
            result.append("")
            continue
        if stripped.startswith(("#", ";")):
            encrypted_comment(stripped[1:])
            continue
        if stripped.startswith("["):
            close = stripped.rfind("]")
            if close < 0:
                raise ValueError()
            tail = stripped[close + 1:].strip()
            if tail:
                if not tail.startswith(("#", ";")):
                    raise ValueError()
                encrypted_comment(tail[1:])
            result.append(stripped[:close + 1])
            continue
        match = re.fullmatch(r'("""[^"]*"""|"[^"]*"|`[^`]*`|[^=:]+)[=:](.*)', stripped)
        if match is None:
            raise ValueError()
        key, value = match.groups()
        key = key.strip()
        if key.startswith('"""'):
            key = key[3:-3]
        elif key.startswith(('"', "`")):
            key = key[1:-1]
        value = value.lstrip()
        quote = '"""' if value.startswith('"""') else "`" if value.startswith("`") else None
        if quote:
            value = value[len(quote):]
            while quote not in value:
                value += "\n" + next(lines)
            close = value.rfind(quote)
            tail = value[close + len(quote):].strip()
            if tail:
                if not tail.startswith(("#", ";")):
                    raise ValueError()
                encrypted_comment(tail[1:])
            value = value[:close]
        else:
            comment = re.search(r"[#;]", value)
            if comment:
                encrypted_comment(value[comment.start() + 1:])
                value = value[:comment.start()]
            value = value.strip()
            if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                value = value[1:-1]
        result.append(key + " = " + value.replace("\n", "\n    "))
    return "\n".join(result)


def verify_sops(data, filename, path):
    # Verify encryption structure without decrypting private authentication
    # payloads. SOPS filestatus additionally checks its native file format.
    try:
        if filename.endswith(".ini"):
            ini = configparser.ConfigParser(interpolation=None, strict=True)
            ini.optionxform = str
            ini.read_string(ini_ciphertext_comments(data.decode("utf-8-sig")))
            if ini.defaults() or "sops" not in ini:
                raise ValueError()
            metadata = dict(ini["sops"])
            payload_sections = [section for section in ini.sections() if section != "sops"]
            if not payload_sections or not any(ini[section] for section in payload_sections):
                raise ValueError()
            for section in ini.sections():
                # SOPS leaves empty INI values unencrypted; they carry no
                # plaintext data and can be meaningful rclone settings.
                if section != "sops" and any(value != "" and not encrypted_value(value)
                                             for value in ini[section].values()):
                    raise ValueError()
            if not encrypted_value(metadata.get("mac")):
                raise ValueError()
            if not isinstance(metadata.get("version"), str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", metadata["version"]):
                raise ValueError()
            allowed = re.compile(r"(age|kms|gcp_kms|azure_kv|hc_vault|pgp)__.*|lastmodified|mac|version|unencrypted_suffix|encrypted_suffix|unencrypted_regex|encrypted_regex|encrypted_comment_regex|unencrypted_comment_regex|mac_only_encrypted")
            if any(not allowed.fullmatch(key) for key in metadata):
                raise ValueError()
            age_keys = [key for key in metadata if re.fullmatch(r"age__list_[0-9]+__map_enc", key)]
            # SOPS flattens multiline metadata into literal \n escapes in INI.
            if not age_keys or any(not age_ciphertext(metadata[key].replace("\\n", "\n")) or
                                   not re.fullmatch(r"age1[a-z0-9]+", metadata.get(key.removesuffix("enc") + "recipient", ""))
                                   for key in age_keys):
                raise ValueError()
        else:
            import yaml
            yaml_ciphertext_comments(data.decode("utf-8-sig"), yaml)
            document = yaml.safe_load(data)
            if not isinstance(document, dict) or not isinstance(document.get("sops"), dict):
                raise ValueError()
            metadata = document["sops"]
            if not encrypted_value(metadata.get("mac")):
                raise ValueError()
            if not isinstance(metadata.get("version"), str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", metadata["version"]):
                raise ValueError()
            allowed = {"age", "kms", "gcp_kms", "azure_kv", "hc_vault", "pgp", "lastmodified", "mac", "version",
                       "unencrypted_suffix", "encrypted_suffix", "unencrypted_regex", "encrypted_regex",
                       "encrypted_comment_regex", "unencrypted_comment_regex", "mac_only_encrypted", "key_groups", "shamir_threshold"}
            if set(metadata) - allowed:
                raise ValueError()
            recipients = metadata.get("age", [])
            if not isinstance(recipients, list) or not recipients:
                raise ValueError()
            if any(not isinstance(item, dict) or set(item) != {"recipient", "enc"} or
                   not isinstance(item["recipient"], str) or not re.fullmatch(r"age1[a-z0-9]+", item["recipient"]) or
                   not age_ciphertext(item["enc"]) for item in recipients):
                raise ValueError()
            def encrypted_tree(value):
                if isinstance(value, dict):
                    return bool(value) and all(isinstance(key, str) and "\n" not in key and not any(marker in key for marker in PRIVATE_MARKERS)
                                               and encrypted_tree(child) for key, child in value.items())
                if isinstance(value, list):
                    return all(encrypted_tree(child) for child in value)
                return encrypted_value(value)
            payload = {key: value for key, value in document.items() if key != "sops"}
            if not payload or not encrypted_tree(payload):
                raise ValueError()
        if any(marker.encode() in data for marker in PRIVATE_MARKERS):
            raise ValueError()
    except ImportError:
        raise ProfileError("PyYAML is required to verify Darwin SOPS ciphertext") from None
    except Exception:
        raise ProfileError("Refusing plaintext or malformed required SOPS ciphertext") from None
    status = parse_json(command(["sops", "filestatus", str(path)], "SOPS ciphertext verification"), "SOPS file status")
    if not isinstance(status, dict) or status.get("encrypted") is not True:
        raise ProfileError("Refusing an unencrypted required SOPS file")
    return data


def image_pins(contract, source):
    feed = contract.definition("imageFeed", source.get("imageFeed"))
    repository = Path(feed["path"])
    if repository.is_symlink() or not repository.is_dir():
        raise ProfileError("Missing or unsafe private image-feed repository")
    revision = feed["revision"]
    resolved = command(["git", "--no-replace-objects", "-C", str(repository), "rev-parse", "--verify", revision + "^{commit}"], "image-feed revision selection").decode().strip()
    if resolved != revision:
        raise ProfileError("Image-feed revision must be an explicit immutable commit")
    pins = parse_json(command(["git", "--no-replace-objects", "-C", str(repository), "show", revision + ":pins.json"], "committed image-pin loading"), "immutable image pins")
    return contract.definition("imagePins", pins)


def trust_certificate(args, selected):
    relative = selected.get("homelab", {}).get("caCertificateFile")
    if relative is None:
        return {}
    trust = args.state_dir / "trust"
    if trust.is_symlink() or not trust.is_dir():
        raise ProfileError("Missing or unsafe public trust directory")
    data = read_file(trust / Path(relative).name, "public trust certificate", 1024 * 1024)
    try:
        text = data.decode("ascii")
        blocks = re.findall(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----", text)
        remainder = text
        for block in blocks:
            remainder = remainder.replace(block, "", 1)
        if not blocks or remainder.strip():
            raise ValueError()
    except (UnicodeError, ValueError):
        raise ProfileError("Public trust must contain only PEM certificates, never private keys") from None
    for block in blocks:
        command(["openssl", "x509", "-noout"], "public certificate validation", block.encode())
    return {relative: data}


def local_file_payloads(source):
    files = source.get("files", {})
    if not isinstance(files, dict):
        raise ProfileError("Invalid local file payload")
    result = {}
    for field, relative in [("vscodeSshConfig", "vscode_ssh_config"), ("syncthingConfig", "syncthing/config.xml")]:
        if field in files:
            if not isinstance(files[field], str):
                raise ProfileError("Invalid local file payload type")
            result[relative] = files[field].encode()
    return result


def backrest_config_path(args):
    override = getattr(args, "backrest_config", None) or os.environ.get("NIX_CONFIG_BACKREST_CONFIG")
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    path = Path(override or state_home / "backrest/config.json").expanduser().absolute()
    try:
        resolved = path.resolve()
        projection = (args.state_dir / ".profile-inputs").resolve()
    except (OSError, RuntimeError):
        raise ProfileError("Missing or unsafe Backrest config") from None
    if resolved.is_relative_to(Path("/nix/store")) or resolved.is_relative_to(projection):
        raise ProfileError("Backrest config must remain outside Nix inputs and stores")
    return path


def backrest_payload(source):
    files = source.get("files", {})
    if not isinstance(files, dict):
        raise ProfileError("Invalid local file payload")
    value = files.get("backrestConfig")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileError("Invalid Backrest config: expected raw UTF-8 JSON")
    try:
        data = value.encode("utf-8")
    except UnicodeError:
        raise ProfileError("Invalid Backrest config: expected raw UTF-8 JSON") from None
    if len(data) > MAX_BYTES:
        raise ProfileError("Invalid Backrest config: file is too large")
    parse_json(value, "Backrest config")
    return data


def read_backrest_config(path):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ProfileError("Missing or unsafe Backrest config")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ProfileError("Invalid Backrest config: expected a bounded regular file")
            data = source.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ProfileError("Invalid Backrest config: file is too large")
            return data
    except FileNotFoundError:
        return None
    except PermissionError:
        # Match the existing sudo workflow, but keep stdout inside this process.
        # Recheck the target under sudo rather than following it with `cat`.
        reader = (
            "import os,stat,sys\n"
            "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)\n"
            "with os.fdopen(fd,'rb') as stream:\n"
            " info=os.fstat(stream.fileno())\n"
            f" if not stat.S_ISREG(info.st_mode) or info.st_size>{MAX_BYTES}: sys.exit(1)\n"
            f" data=stream.read({MAX_BYTES + 1})\n"
            f" if len(data)>{MAX_BYTES}: sys.exit(1)\n"
            " sys.stdout.buffer.write(data)\n"
        )
        return command(["sudo", "--", sys.executable, "-c", reader, str(path)],
                       "privileged Backrest config read")
    except OSError:
        raise ProfileError("Missing or unsafe Backrest config") from None


def capture_backrest(source, args):
    current = read_backrest_config(backrest_config_path(args))
    if current is not None:
        try:
            value = current.decode("utf-8")
        except UnicodeError:
            raise ProfileError("Invalid Backrest config: expected raw UTF-8 JSON") from None
        files = source.get("files", {})
        if not isinstance(files, dict):
            raise ProfileError("Invalid local file payload")
        source["files"] = dict(files, backrestConfig=value)
    backrest_payload(source)


def compare_local(path, expected, args, label, as_json=False):
    if args.force:
        return
    if not path.exists() and not path.is_symlink():
        return
    current = read_file(path, label)
    if as_json:
        current = json_bytes(parse_json(current, label))
    if current != expected and not args.force:
        raise ProfileError("Refusing to overwrite locally changed " + label + "; seal first or use --force")


def current_input(args):
    path = args.state_dir / "input"
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_symlink():
        raise ProfileError("Refusing to replace an unmanaged input directory")
    target = Path(os.readlink(path))
    if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != ".profile-inputs" or not re.fullmatch(r"[0-9a-f]{32}", target.parts[1]):
        raise ProfileError("Refusing an unsafe rendered input link")
    generations = args.state_dir / ".profile-inputs"
    directory = args.state_dir / target
    if generations.is_symlink() or directory.is_symlink() or not directory.is_dir():
        raise ProfileError("Refusing an unsafe rendered input generation")
    return directory


def check_input_local(args):
    previous = current_input(args)
    if previous is not None and not args.force:
        manifest = parse_json(read_file(previous.parent / (previous.name + ".json"), "rendered input manifest"),
                              "rendered input manifest")
        existing = {}
        for directory, subdirs, filenames in os.walk(previous, followlinks=False):
            root = Path(directory)
            if any((root / name).is_symlink() for name in subdirs):
                raise ProfileError("Refusing unsafe locally changed rendered input")
            for name in filenames:
                path = root / name
                existing[str(path.relative_to(previous))] = hashlib.sha256(read_file(path, "rendered input file")).hexdigest()
        if existing != manifest:
            raise ProfileError("Refusing to overwrite locally changed rendered input; seal first or use --force")


def publish_input(args, files):
    check_input_local(args)
    generations = args.state_dir / ".profile-inputs"
    secure_directory(generations)
    generation = uuid.uuid4().hex
    destination = generations / generation
    destination.mkdir(mode=0o700)
    temporary_link = args.state_dir / (".input-" + generation)
    try:
        for relative, data in files.items():
            atomic_file(destination / relative, data)
        atomic_file(generations / (generation + ".json"),
                    json_bytes({relative: hashlib.sha256(data).hexdigest() for relative, data in files.items()}))
        os.symlink(str(Path(".profile-inputs") / generation), temporary_link)
        os.replace(temporary_link, args.state_dir / "input")
    except BaseException:
        shutil.rmtree(destination)
        with contextlib.suppress(FileNotFoundError):
            (generations / (generation + ".json")).unlink()
        with contextlib.suppress(FileNotFoundError):
            temporary_link.unlink()
        raise
    # Existing generations remain intact for concurrent Nix readers. They
    # contain only approved projected files, not the surrounding private state.


def render(args, contract, source):
    projected = contract.project(source, args.platform)
    selected = projected["profiles"][args.platform]
    if args.platform == "nixos":
        source = projected | {"imageFeed": contract.definition("imageFeed", source.get("imageFeed"))}
    files = {"profile.json": json_bytes(projected)}
    files.update(trust_certificate(args, selected))
    if args.platform == "nixos":
        files["image-pins.json"] = json_bytes(image_pins(contract, source))
        files["image-feed.json"] = json_bytes({"path": source["imageFeed"]["path"]})
    else:
        for filename in ["secrets.sops.yaml", "rclone.ini"]:
            path = args.state_dir / filename
            files[filename] = verify_sops(read_file(path, "required Darwin SOPS ciphertext"), filename, path)
    pins_output = "\n      imagePins = (builtins.fromJSON (builtins.readFile ./image-pins.json)).images;" if args.platform == "nixos" else ""
    feed_output = "\n      imageFeedPath = (builtins.fromJSON (builtins.readFile ./image-feed.json)).path;" if args.platform == "nixos" else ""
    files["flake.nix"] = ("{\n  description = \"Locally projected machine profile\";\n  outputs = { self }: {\n"
                          "      profiles = (builtins.fromJSON (builtins.readFile ./profile.json)).profiles;" + pins_output + feed_output +
                          "\n  };\n}\n").encode()
    local = local_file_payloads(source) if args.platform == "darwin" else {}
    backrest = backrest_payload(source) if args.platform == "darwin" else None
    backrest_path = None
    if backrest is not None:
        backrest_path = backrest_config_path(args)
        current = read_backrest_config(backrest_path)
        if current is not None and current != backrest and not args.force:
            raise ProfileError("Refusing to overwrite locally changed Backrest config; seal first or use --force")
    compare_local(args.state_dir / "profile.json", json_bytes(source), args, "profile metadata", as_json=True)
    for relative, data in local.items():
        compare_local(args.state_dir / relative, data, args, "local file")
    publish_input(args, files)
    atomic_file(args.state_dir / "profile.json", json_bytes(source))
    for relative, data in local.items():
        atomic_file(args.state_dir / relative, data)
    if backrest_path is not None:
        atomic_file(backrest_path, backrest)
    # Nix path inputs archive a root symlink rather than following it. Give
    # callers the immutable directory, not the atomic convenience link.
    return current_input(args)


def source_for_seal(args):
    source = parse_json(read_file(args.bootstrap or args.state_dir / "profile.json", "unsealed profile metadata"), "unsealed profile metadata")
    if not isinstance(source, dict):
        fail("profiles", "expected a platform profile map")
    if args.platform == "darwin" and not args.bootstrap:
        files = source.get("files", {})
        if not isinstance(files, dict):
            raise ProfileError("Invalid local file payload")
        files = dict(files)
        for field, relative in [("vscodeSshConfig", "vscode_ssh_config"), ("syncthingConfig", "syncthing/config.xml")]:
            path = args.state_dir / relative
            if field in files or path.exists():
                try:
                    files[field] = read_file(path, "local file").decode()
                except UnicodeError:
                    raise ProfileError("Invalid local file encoding") from None
        if files:
            source["files"] = files
    if args.platform == "darwin":
        capture_backrest(source, args)
    return source


def seal(args, contract, source):
    projected = contract.project(source, args.platform)
    if args.platform == "nixos":
        source = projected | {"imageFeed": contract.definition("imageFeed", source.get("imageFeed"))}
        image_pins(contract, source)
    else:
        # Validate editable local files; the full payload stays outside Nix.
        local_file_payloads(source)
        backrest_payload(source)
    ciphertext = seal_bytes(args, source)
    if args.platform == "nixos":
        recipient = source["profiles"]["nixos"]["homelab"]["backup"]["ageRecipient"]
        portable = command(["age", "--encrypt", "--recipient", recipient], "portable profile backup encryption",
                           json_bytes(source))
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = args.state_dir / "backups" / ("profile." + timestamp + "." + uuid.uuid4().hex + ".json.age")
        # Commit the portable encrypted snapshot before replacing the local
        # TPM ciphertext. A successful seal is always recoverable with the
        # existing backup identity; no identity is generated or rotated.
        atomic_file(backup, portable, exclusive=True)
    atomic_file(backend_path(args), ciphertext)
    atomic_file(args.state_dir / "profile.json", json_bytes(source))
    return source


def edit(args, contract):
    # The caller holds the state lock through rendering, the interactive editor
    # and sealing, so another profile operation cannot replace this transaction.
    # Render's normal drift checks also protect local metadata and Backrest edits.
    render(args, contract, load_sealed(args))
    try:
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nvim")
    except ValueError:
        raise ProfileError("Invalid editor command; editable metadata retained") from None
    if not editor:
        raise ProfileError("Invalid editor command; editable metadata retained")
    try:
        # Preserve the terminal for interactive editors, but never relay editor
        # diagnostics or interpolate its command through a shell.
        result = subprocess.run([*editor, str(args.state_dir / "profile.json")],
                                stderr=subprocess.DEVNULL)
    except (OSError, ValueError):
        raise ProfileError("Unavailable editor; editable metadata retained") from None
    if result.returncode:
        raise ProfileError("Editor failed; editable metadata retained")
    seal(args, contract, source_for_seal(args))


def parser():
    root = argparse.ArgumentParser(description=__doc__,
                                   epilog="The 'profile python SCRIPT [ARGS...]' command uses the bundled interpreter.")
    root.add_argument("operation", choices=["render", "edit", "seal", "update-images", "project", "query"])
    root.add_argument("--platform", choices=["darwin", "nixos"], default="darwin" if sys.platform == "darwin" else "nixos")
    root.add_argument("--state-dir", type=Path, default=Path(os.environ.get("NIX_CONFIG_STATE_DIR") or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "nix-config"))
    root.add_argument("--backrest-config", type=Path, help="Private runtime Backrest JSON (defaults to NIX_CONFIG_BACKREST_CONFIG or XDG state/backrest/config.json, not --state-dir)")
    root.add_argument("--backend", choices=["sops", "systemd-creds"])
    root.add_argument("--identity", type=Path, help="Existing SOPS identity; no key is generated")
    root.add_argument("--bootstrap", type=Path, help="Explicit unsealed bootstrap JSON; never a silent fallback")
    root.add_argument("--force", action="store_true", help="Replace local render edits intentionally")
    root.add_argument("--image-feed", type=Path, help="Private Git image-feed repository for update-images")
    root.add_argument("--revision", help="Full immutable commit ID for update-images")
    root.add_argument("--output", type=Path, help="Private output JSON for project")
    root.add_argument("--field", choices=["hostname", "checkout", "input"], help="Non-secret system field or physical narrow input directory")
    return root


def main():
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == "python":
        if len(sys.argv) < 3:
            print("Usage: profile python SCRIPT [ARGS...]", file=sys.stderr)
            return 2
        os.environ["PROFILE_HELPER_PYTHON"] = str(Path(__file__).resolve())
        os.execv(sys.executable, [sys.executable, *sys.argv[2:]])
    arguments = parser()
    args = arguments.parse_args()
    args.state_dir = args.state_dir.expanduser().absolute()
    args.backend = args.backend or ("sops" if args.platform == "darwin" else "systemd-creds")
    if (args.platform, args.backend) not in [("darwin", "sops"), ("nixos", "systemd-creds")]:
        arguments.error("Darwin requires SOPS; Linux requires existing user/host TPM credentials")
    if args.operation == "update-images" and (args.platform != "nixos" or not args.image_feed or not args.revision):
        arguments.error("update-images requires --platform nixos --image-feed PATH --revision COMMIT")
    if args.operation != "update-images" and (args.image_feed or args.revision):
        arguments.error("image-feed revision changes require update-images")
    if args.operation == "project" and (not args.bootstrap or not args.output):
        arguments.error("project requires explicit --bootstrap JSON and --output JSON")
    if args.operation != "project" and args.output:
        arguments.error("--output is only available for project")
    if args.operation == "query" and not args.field:
        arguments.error("query requires --field hostname|checkout|input")
    if args.operation != "query" and args.field:
        arguments.error("--field is only available for query")
    if args.operation == "edit" and (args.bootstrap or args.force):
        arguments.error("edit requires sealed metadata and refuses --bootstrap or --force")
    try:
        contract = Contract()
        if args.operation == "project":
            source = parse_json(read_file(args.bootstrap, "bootstrap metadata"), "bootstrap metadata")
            atomic_file(args.output.expanduser().absolute(), json_bytes(contract.project(source, args.platform)))
            print(args.output)
            return 0
        if args.operation == "query":
            directory = current_input(args)
            if directory is None:
                raise ProfileError("Missing rendered profile input; render first")
            check_input_local(args)
            source = parse_json(read_file(directory / "profile.json", "rendered profile metadata"),
                                "rendered profile metadata")
            selected = contract.project(source, args.platform)["profiles"][args.platform]
            print(directory if args.field == "input" else selected["system"][args.field])
            return 0
        secure_directory(args.state_dir)
        lock_fd = os.open(args.state_dir / ".profile.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, "a") as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise ProfileError("Refusing an unsafe private state lock")
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if args.operation == "render":
                source = parse_json(read_file(args.bootstrap, "bootstrap metadata"), "bootstrap metadata") if args.bootstrap else load_sealed(args)
                print(render(args, contract, source))
            elif args.operation == "seal":
                seal(args, contract, source_for_seal(args))
                print(backend_path(args))
            elif args.operation == "edit":
                edit(args, contract)
                print(backend_path(args))
            else:
                source = source_for_seal(args)
                source["imageFeed"] = {"path": str(args.image_feed.expanduser().absolute()), "revision": args.revision}
                # Complete validation and local-edit checks before replacing
                # the encrypted metadata or changing the selected revision.
                contract.project(source, args.platform)
                image_pins(contract, source)
                check_input_local(args)
                source = seal(args, contract, source)
                print(render(args, contract, source))
        return 0
    except (ProfileError, OSError, RecursionError):
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, ProfileError) else "Profile operation failed; private filesystem details omitted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
