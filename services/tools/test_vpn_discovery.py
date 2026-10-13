import importlib.util
from pathlib import Path
import socket
import struct
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("vpn_ddns", Path(__file__).with_name("vpn-ddns.py"))
ddns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ddns)


class DiscoveryTests(unittest.TestCase):
    def response(self, address, transaction=b"123456789012"):
        family = socket.AF_INET if ":" not in address else socket.AF_INET6
        raw = socket.inet_pton(family, address)
        mask = struct.pack("!I", ddns.COOKIE) + transaction
        data = bytes((0, 1 if family == socket.AF_INET else 2)) + b"\x00\x00"
        data += bytes(a ^ b for a, b in zip(raw, mask))
        attribute = struct.pack("!HH", 0x20, len(data)) + data
        return struct.pack("!HHI", 0x101, len(attribute), ddns.COOKIE) + transaction + attribute

    def test_xor_mapped_ipv4_and_ipv6(self):
        for address, version in [("1.1.1.1", 4), ("2606:4700:4700::1111", 6)]:
            with self.subTest(version=version):
                self.assertEqual(ddns.mapped_address(self.response(address), b"123456789012", version), address)

    def test_wrong_transaction_family_or_truncated_response_rejected(self):
        packet = self.response("1.1.1.1")
        for response, transaction, family in [(packet, b"000000000000", 4), (packet, b"123456789012", 6), (packet[:-1], b"123456789012", 4)]:
            with self.subTest(response=response, transaction=transaction, family=family), self.assertRaises(ValueError):
                ddns.mapped_address(response, transaction, family)

    def test_disagreement_or_only_one_provider_does_not_publish(self):
        for results in ({}, {"Google": "1.1.1.1"}, {"Google": "1.1.1.1", "Microsoft": "8.8.8.8"}):
            with self.subTest(results=results), self.assertRaises(ddns.DDNSError):
                ddns.agreed_address(results, 4)
        self.assertEqual(ddns.agreed_address({"Google": "1.1.1.1", "Microsoft": "1.1.1.1"}, 4), "1.1.1.1")

    def test_router_identifier_is_fixed_while_prefix_changes(self):
        cases = [
            ("2606:4700:1234:1000:49a0:b10:1bfb:6872", "2606:4700:1234:1000::abcd:1234"),
            ("2606:4700:1234:1000:ffff:ffff:ffff:ffff", "2606:4700:1234:1000::abcd:1234"),
            ("2606:4700:5678:ab00::1234", "2606:4700:5678:ab00::abcd:1234"),
        ]
        for observed, expected in cases:
            with self.subTest(observed=observed):
                self.assertEqual(ddns.router_ipv6_address(observed, ddns.router_identifier("::abcd:1234")), expected)

    def test_discovery_publishes_router_address_not_host_address(self):
        observed = "2606:4700:1234:1000:49a0:b10:1bfb:6872"
        with patch.object(ddns, "stable_ipv6_source", return_value=observed), patch.object(
            ddns, "stun_address", side_effect=lambda host, port, version, source=None: "1.1.1.1" if version == 4 else observed
        ):
            addresses, results = ddns.discover(ddns.router_identifier("::abcd:1234"))
        self.assertEqual(addresses, {"A": "1.1.1.1", "AAAA": "2606:4700:1234:1000::abcd:1234"})
        self.assertEqual(results[6], {"Google": observed, "Microsoft": observed})

    def test_ipv6_ignores_temporary_deprecated_and_tentative_sources(self):
        import json
        route = [{"dev": "eth0", "prefsrc": "2606:4700::1"}]
        addresses = [{"addr_info": [
            {"local": "2606:4700::1", "scope": "global", "preferred_life_time": 600, "temporary": True},
            {"local": "2606:4700::2", "scope": "global", "preferred_life_time": 0, "deprecated": True},
            {"local": "2606:4700::3", "scope": "global", "preferred_life_time": 600, "tentative": True},
            {"local": "2606:4700::4", "scope": "global", "preferred_life_time": 600},
        ]}]
        with patch.object(ddns.subprocess, "check_output", side_effect=[json.dumps(route), json.dumps(addresses)]):
            self.assertEqual(ddns.stable_ipv6_source(), "2606:4700::4")


if __name__ == "__main__":
    unittest.main()
