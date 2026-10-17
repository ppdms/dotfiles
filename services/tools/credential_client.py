"""Private Unix-socket protocol; the server authenticates the actual peer UID."""

from __future__ import annotations

import base64
import json
import os
import socket

MAX_MESSAGE = 16 * 1024 * 1024
ERRORS = {
    "denied": "Credential operation is not authorized for this Unix account",
    "invalid": "Credential request or record is invalid",
    "conflict": "Encrypted credentials changed concurrently; no update saved",
    "unavailable": "Encrypted credential operation failed; pre-update record retained",
    "rollback": "Credential save outcome is uncertain; encrypted pre-update backup retained",
}


class CredentialError(RuntimeError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate protocol key")
        result[key] = value
    return result


def request(operation, component, **arguments):
    message = json.dumps({"operation": operation, "component": component, **arguments},
                         ensure_ascii=True, allow_nan=False).encode() + b"\n"
    if len(message) > MAX_MESSAGE:
        raise CredentialError("Credential request is too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(180)
            connection.connect(os.environ.get("HOMELAB_CREDENTIAL_SOCKET", "/run/homelab-credentials/control.sock"))
            connection.sendall(message)
            with connection.makefile("rb") as stream:
                response = stream.readline(MAX_MESSAGE + 1)
        if len(response) > MAX_MESSAGE or not response.endswith(b"\n"):
            raise ValueError
        result = json.loads(response, object_pairs_hook=_unique)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError
        if not result["ok"]:
            raise CredentialError(ERRORS.get(result.get("error"), "Credential broker rejected the request"))
        return result
    except CredentialError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise CredentialError("Credential broker is unavailable or returned an invalid response") from None


def decode_bytes(value):
    try:
        return base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        raise CredentialError("Credential broker returned invalid encoded data") from None


def read_record(component, complete=False):
    # complete=True asks the broker to run full content validation
    # (credential_validation) server-side before returning the record.
    result = request("read", component, complete=True) if complete else request("read", component)
    value = result.get("payload")
    return (None if value is None else decode_bytes(value), result.get("digest"))


def store_record(component, payload, expected_digest):
    return request("store", component, payload=base64.b64encode(payload).decode("ascii"),
                   expected_digest=expected_digest)


def update_mutable(component, field, value, expected_digest):
    return request("mutable", component, field=field, value=value, expected_digest=expected_digest)


def export_record(component):
    value = request("export", component).get("ciphertext")
    return None if value is None else decode_bytes(value)
