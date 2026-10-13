"""Change-only Cloudflare DNS synchronization; state contains no credentials."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import subprocess


class DDNSError(Exception):
    """A diagnostic that never includes raw credential-bearing process output."""


def public_address(value: str, version: int) -> str:
    address = ipaddress.ip_address(value.strip())
    if address.version != version or not address.is_global or address.is_multicast:
        raise ValueError("not a public address of the requested family")
    return str(address)


class Cloudflare:
    def __init__(self, zone: str, hostname: str, token: str):
        self.zone = zone
        self.hostname = hostname
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("CF_", "CLOUDFLARE_"))
        }
        self.environment["CLOUDFLARE_API_TOKEN"] = token

    def command(self, *arguments):
        try:
            result = subprocess.run(
                [os.environ["HOMELAB_CF"], "dns", "records", *arguments,
                 "-z", self.zone],
                env=self.environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12,
            )
            if result.returncode:
                raise DDNSError("Cloudflare request failed; check the token's DNS permissions")
            return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise DDNSError("Cloudflare request failed or returned invalid JSON") from None

    def records(self):
        records = self.command("list", "--name", self.hostname, "--per-page", "100")
        if not isinstance(records, list):
            raise DDNSError("Cloudflare returned an invalid record list")
        return records

    def update(self, record, address):
        # PATCH only content: TTL, proxy setting, comments and tags are retained.
        result = self.command("edit", record["id"], "--body", json.dumps({"content": address}))
        if not isinstance(result, dict) or result.get("content") != address or result.get("id") != record["id"]:
            raise DDNSError("Cloudflare did not confirm the DNS record update")


def sync_addresses(addresses, state, cloudflare, save):
    """Persist each confirmed family separately; a failed family retries next run."""
    pending = {kind: address for kind, address in addresses.items() if state.get(kind) != address}
    if not pending:
        return []
    records = cloudflare.records()
    selected = {}
    # Validate all target records before making any mutations.
    for kind in pending:
        matches = [record for record in records
                   if record.get("name") == cloudflare.hostname and record.get("type") == kind]
        if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
            raise DDNSError(f"Expected exactly one existing {kind} record; no records created or deleted")
        record = matches[0]
        if record.get("proxied") is not False:
            raise DDNSError(f"The {kind} record must be DNS-only for the VPN")
        selected[kind] = record
    changed = []
    for kind, address in pending.items():
        record = selected[kind]
        if record.get("content") != address:
            cloudflare.update(record, address)
            changed.append(kind)
        state[kind] = address
        save(state)
    return changed


def read_state(path: Path, zone: str, hostname: str) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise DDNSError("Cannot read DDNS state; refusing to discard it") from None
    if not isinstance(data, dict) or data.get("zone") != zone or data.get("hostname") != hostname:
        return {}
    addresses = data.get("addresses")
    if not isinstance(addresses, dict):
        raise DDNSError("Invalid DDNS address cache")
    try:
        return {kind: public_address(value, {"A": 4, "AAAA": 6}[kind]) for kind, value in addresses.items()}
    except (KeyError, ValueError, AttributeError, TypeError):
        raise DDNSError("Invalid DDNS address cache") from None


def write_state(path: Path, zone: str, hostname: str, addresses: dict):
    temporary = path.with_suffix(".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"zone": zone, "hostname": hostname, "addresses": addresses}, stream)
        stream.write("\n")
    os.replace(temporary, path)
