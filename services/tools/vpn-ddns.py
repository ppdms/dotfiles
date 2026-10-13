#!/usr/bin/env python3
"""Discover the host's public addresses and maintain the router's VPN DNS records."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
from vpn_dns import Cloudflare, DDNSError, public_address, read_state, sync_addresses, write_state


COOKIE = 0x2112A442


def router_identifier(value):
    """Parse the required router IPv6 interface identifier, not a full address."""
    try:
        identifier = int(ipaddress.IPv6Address(value))
        if identifier >= 1 << 64:
            raise ValueError("identifier includes a network prefix")
        return identifier
    except ValueError:
        raise argparse.ArgumentTypeError("router interface identifier must be a low-64-bit IPv6 value") from None


def stable_ipv6_source():
    """Use a stable address on the default-route interface, not a privacy address."""
    try:
        route = json.loads(subprocess.check_output(
            ["ip", "-j", "-6", "route", "get", "2001:4860:4860::8888"],
            stderr=subprocess.DEVNULL, timeout=2,
        ))[0]
        interfaces = json.loads(subprocess.check_output(
            ["ip", "-j", "-6", "address", "show", "dev", route["dev"]],
            stderr=subprocess.DEVNULL, timeout=2,
        ))
        candidates = []
        for interface in interfaces:
            for address in interface["addr_info"]:
                if (address.get("scope") == "global"
                    and not any(address.get(flag) for flag in ("temporary", "deprecated", "tentative", "dadfailed"))
                    and address.get("preferred_life_time", 0) != 0):
                    candidates.append(public_address(address["local"], 6))
        if route.get("prefsrc") in candidates:
            return route["prefsrc"]
        return sorted(candidates)[0] if candidates else None
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        return None


def mapped_address(packet, transaction, version):
    if len(packet) < 20:
        raise ValueError("short STUN response")
    kind, size, cookie = struct.unpack("!HHI", packet[:8])
    if kind != 0x101 or cookie != COOKIE or packet[8:20] != transaction or size % 4 or len(packet) != 20 + size:
        raise ValueError("invalid STUN response")
    offset = 20
    mapped = None
    while offset < len(packet):
        tag, length = struct.unpack("!HH", packet[offset:offset + 4])
        end = offset + 4 + length
        if end > len(packet):
            raise ValueError("truncated STUN attribute")
        data = packet[offset + 4:end]
        offset += 4 + ((length + 3) // 4) * 4
        if tag not in (0x0001, 0x0020):
            continue
        expected_family = 1 if version == 4 else 2
        expected_size = 8 if version == 4 else 20
        if len(data) != expected_size or data[:2] != bytes((0, expected_family)):
            raise ValueError("wrong mapped address family")
        raw = data[4:]
        if tag == 0x0020:
            mask = struct.pack("!I", COOKIE) + transaction
            raw = bytes(left ^ right for left, right in zip(raw, mask))
        address = public_address(socket.inet_ntop(socket.AF_INET if version == 4 else socket.AF_INET6, raw), version)
        if tag == 0x0020:
            return address
        mapped = address
    if mapped is None:
        raise ValueError("no STUN mapped address")
    return mapped


def stun_address(host, port, version, source=None):
    family = socket.AF_INET if version == 4 else socket.AF_INET6
    transaction = secrets.token_bytes(12)
    packet = struct.pack("!HHI", 1, 0, COOKIE) + transaction
    destination = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM)[0][4]
    with socket.socket(family, socket.SOCK_DGRAM) as connection:
        connection.settimeout(4)
        if source:
            connection.bind((source, 0))
        connection.connect(destination)
        connection.send(packet)
        return mapped_address(connection.recv(2048), transaction, version)


def agreed_address(results, version):
    values = list(results.values())
    if len(values) < 2 or len(set(values)) != 1:
        raise DDNSError(f"IPv{version}: insufficient agreement between public-address providers; DNS left unchanged")
    return values[0]


def router_ipv6_address(observed_address, router_interface_id):
    """Replace the host identifier with the router's, retaining the observed /64."""
    prefix = ipaddress.IPv6Network(f"{public_address(observed_address, 6)}/64", strict=False)
    return str(ipaddress.IPv6Address(int(prefix.network_address) | router_interface_id))


def discover(router_interface_id):
    source = stable_ipv6_source()
    jobs = {}
    for name, host, port in (
        ("Google", "stun.l.google.com", 19302),
        ("Microsoft", "worldaz.tr.teams.microsoft.com", 3478),
    ):
        jobs[(name, 4)] = (stun_address, (host, port, 4))
        if source:
            jobs[(name, 6)] = (stun_address, (host, port, 6, source))
    results = {4: {}, 6: {}}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {key: pool.submit(function, *args) for key, (function, args) in jobs.items()}
        for (name, version), future in futures.items():
            try:
                results[version][name] = future.result()
            except (OSError, ValueError, subprocess.SubprocessError):
                print(f"IPv{version}: {name} discovery unavailable", file=sys.stderr)
    addresses = {}
    for version, kind in ((4, "A"), (6, "AAAA")):
        try:
            observed = agreed_address(results[version], version)
            addresses[kind] = router_ipv6_address(observed, router_interface_id) if version == 6 else observed
        except DDNSError as error:
            print(str(error), file=sys.stderr)
    return addresses, results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--router-interface-id", required=True, type=router_identifier,
                        help="Router's fixed low-64-bit IPv6 identifier; the observed /64 prefix remains dynamic")
    parser.add_argument("--discover-only", action="store_true", help="probe addresses without credentials or Cloudflare calls")
    args = parser.parse_args()
    os.umask(0o077)
    if args.discover_only:
        addresses, results = discover(args.router_interface_id)
        print(json.dumps({"addresses": addresses, "providers": results}, indent=2))
        return 0 if addresses else 1
    import homelab_secrets

    # Read fresh credentials each minute: saving a token needs no restart.
    fields = homelab_secrets.load_fields("vpn-ddns")
    token = fields.get("api-token")
    if not token:
        print("Waiting for credentials: run creds vpn-ddns")
        return 0
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "homelab-vpn-ddns"
    homelab_secrets.private_directory(root)
    with open(root / ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        path = root / "addresses.json"
        state = read_state(path, args.zone, args.hostname)
        addresses, _ = discover(args.router_interface_id)
        if not addresses:
            raise DDNSError("No confirmed public addresses; DNS left unchanged")
        changed = sync_addresses(
            addresses, state, Cloudflare(args.zone, args.hostname, token),
            lambda value: write_state(path, args.zone, args.hostname, value),
        )
        if changed:
            print(args.hostname + ": updated " + ", ".join(changed))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DDNSError as error:
        print("vpn-ddns: " + str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        # Never log credential contents, subprocess diagnostics or tracebacks.
        print("vpn-ddns: failed; check keyring, network and local state access", file=sys.stderr)
        raise SystemExit(1)
