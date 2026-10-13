import unittest

from vpn_dns import DDNSError, public_address, sync_addresses


class DNS:
    hostname = "vpn.example.org"

    def __init__(self):
        self.calls = []
        self.fail = None
        self.rows = [
            {"id": "v4", "name": self.hostname, "type": "A", "content": "8.8.8.8", "proxied": False},
            {"id": "v6", "name": self.hostname, "type": "AAAA", "content": "2001:4860:4860::8888", "proxied": False},
        ]

    def records(self):
        self.calls.append("list")
        return self.rows

    def update(self, record, address):
        self.calls.append((record["id"], address))
        if record["type"] == self.fail:
            raise DDNSError("update failed")
        record["content"] = address


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.dns = DNS()
        self.saved = []
        self.save = lambda value: self.saved.append(dict(value))

    def test_bootstrap_then_unchanged_makes_no_cloudflare_calls(self):
        addresses = {"A": "8.8.8.8", "AAAA": "2001:4860:4860::8888"}
        state = {}
        self.assertEqual(sync_addresses(addresses, state, self.dns, self.save), [])
        self.dns.calls.clear()
        sync_addresses(addresses, state, self.dns, self.save)
        self.assertEqual(self.dns.calls, [])
        self.assertEqual(state, addresses)

    def test_failed_second_family_keeps_first_confirmed_and_retries(self):
        addresses = {"A": "1.1.1.1", "AAAA": "2606:4700:4700::1111"}
        state = {}
        self.dns.fail = "AAAA"
        with self.assertRaises(DDNSError):
            sync_addresses(addresses, state, self.dns, self.save)
        self.assertEqual(self.saved, [{"A": "1.1.1.1"}])
        self.dns.fail = None
        self.dns.calls.clear()
        sync_addresses(addresses, state, self.dns, self.save)
        self.assertEqual(self.dns.calls, ["list", ("v6", addresses["AAAA"])])
        self.assertEqual(state, addresses)

    def test_unavailable_ipv6_is_preserved(self):
        state = {"A": "8.8.8.8", "AAAA": "2001:4860:4860::8888"}
        sync_addresses({"A": "1.1.1.1"}, state, self.dns, self.save)
        self.assertEqual(state["AAAA"], "2001:4860:4860::8888")
        self.assertEqual(self.dns.calls, ["list", ("v4", "1.1.1.1")])

    def test_missing_duplicate_or_proxied_record_prevents_any_mutation(self):
        for mode in ("missing", "duplicate", "proxied"):
            with self.subTest(mode=mode):
                dns = DNS()
                if mode == "missing":
                    dns.rows.pop()
                elif mode == "duplicate":
                    dns.rows.append(dict(dns.rows[1]))
                else:
                    dns.rows[1]["proxied"] = True
                with self.assertRaises(DDNSError):
                    sync_addresses({"A": "1.1.1.1", "AAAA": "2606:4700:4700::1111"}, {}, dns, self.save)
                self.assertEqual(dns.calls, ["list"])
                self.assertEqual(self.saved, [])

    def test_private_wrong_family_and_multicast_addresses_rejected(self):
        for value, family in [("192.168.1.10", 4), ("::1", 6), ("fe80::1", 6), ("224.0.0.1", 4), ("8.8.8.8", 6)]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                public_address(value, family)


if __name__ == "__main__":
    unittest.main()
