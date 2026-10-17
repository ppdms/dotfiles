"""Credential record layout: component/field catalog and payload envelope.

This module is the only credential code the Quadlet-side client may import.
It contains no validation logic (no hashes, YAML, TLS, or subprocess), so
editing credential_validation.py never changes the client package's store
path and therefore never restarts containers.
"""

from __future__ import annotations

import json

FIELDS = {
    "backrest": {"restic-password": "restic/restic-password", "rclone.conf": "rclone/rclone.conf"},
    "authelia": {name: name for name in ("jwt-secret", "session-secret", "storage-encryption-key", "users.yml", "oidc-issuer.pem")},
    "authelia-oidc": {name: name for name in ("forgejo", "immich", "jellyfin", "termix", "kavita", "readeck")},
    "nginx": {name: name for name in ("tls.crt", "tls.key", "tls-public.crt", "tls-public.key", "omp-web-token.conf")},
    "alloy": {name: "env/" + name for name in ("GRAFANA_CLOUD_LOKI_URL", "GRAFANA_CLOUD_LOKI_USER", "GRAFANA_CLOUD_API_KEY", "GRAFANA_CLOUD_PROM_URL", "GRAFANA_CLOUD_PROM_USER", "GRAFANA_CLOUD_READ_TOKEN")},
    "immich": {name: name for name in ("postgres-password", "db-username", "db-password", "db-name")},
    "cloudflared": {"tunnel-token": "tunnel-token"},
    "opencloud": {"opencloud.yaml": "opencloud.yaml"},
    "termix": {"oidc-client-secret": "oidc-client-secret"},
    "transmission": {"wireguard-private-key": "wireguard-private-key"},
    "kavita": {"oidc-client-secret": "oidc-client-secret"},
    # Broker-delivered OIDC client secret for the readeck principal;
    # Authelia holds the matching verifier under authelia-oidc.
    "readeck": {"oidc-client-secret": "oidc-client-secret"},
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


def required_fields(component: str) -> list[str]:
    return [name for name in FIELDS[component] if name not in OPTIONAL.get(component, ())]


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


def decode_payload(component: str, data: bytes) -> dict[str, str]:
    """Parse the version-1 envelope and check field shape only.

    Content validation (hashes, YAML, TLS pairs, ...) lives in
    credential_validation and runs on the broker and operator side.
    """
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
    _field_shape(component, fields, complete=False)
    return fields


def encode_payload(component: str, fields: dict[str, str]) -> bytes:
    """Serialize deterministically, including incomplete editor templates."""
    _field_shape(component, fields, complete=False)
    payload = {"version": 1, "services": {component: {"env": {}, "files": fields}}}
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
