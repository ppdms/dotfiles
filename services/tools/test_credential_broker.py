"""Consumer-visible authorization, concurrent update, rollback and seed-import regressions.

FixtureCipher isolates store transaction tests from the host TPM; native UID-bound
systemd-creds encryption is exercised separately by the integration owner.
"""

import base64
import json
import multiprocessing
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from credential_broker import Broker, BrokerError, CredentialServer, EncryptedStore, Policy, RequestHandler, digest, import_seed, notify_ready, remove_stale_socket
from credential_validation import parse_payload, payload_bytes


class FixtureCipher:
    def __init__(self):
        self.records = {}

    def encrypt(self, component, value):
        encrypted = b"fixture-ciphertext:" + digest(value).encode()
        self.records[(component, encrypted)] = value
        return encrypted

    def decrypt(self, component, encrypted):
        return self.records[(component, encrypted)]

    def seed_encrypt(self, component, value):
        return self.encrypt(component, value)

    def seed_decrypt(self, component, encrypted):
        return self.decrypt(component, encrypted)

    def export(self, recipient, value):
        return b"age-encryption.org/v1\nfixture-only\n" + digest(value).encode()


def registry(backrest_uid=43002, operator_uid=1000):
    return {
        "operator": {"uid": operator_uid},
        "definitions": {"credentials": {"uid": 43021}},
        "principals": {
            "backrest": {"uid": backrest_uid, "components": ["backrest"]},
            "authelia": {"uid": 43001, "components": ["authelia", "authelia-oidc"]},
            "cloudflared": {"uid": 43003, "components": ["cloudflared"]},
            "nginx": {"uid": 43010, "components": ["nginx"]},
            "vpn-ddns": {"uid": 43019, "components": ["vpn-ddns"]},
            "credentials": {"uid": 43021, "components": []},
            "secrets-export": {"uid": 43022, "components": []},
        },
        "backup": {"ageRecipient": "original-fixture-recipient"},
    }


def request(operation, component, **fields):
    return {"operation": operation, "component": component, **fields}


def peer_requests(path, messages, uid, channel):
    try:
        if os.getuid() == 0:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
        results = []
        for message in messages:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(10)
                client.connect(path)
                client.sendall(json.dumps(message).encode() + b"\n")
                with client.makefile("rb") as stream:
                    results.append(json.loads(stream.readline()))
        channel.send(results)
    finally:
        channel.close()


class CredentialBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.crypto = FixtureCipher()
        self.store = EncryptedStore(self.root / "encrypted", self.crypto)
        self.broker = Broker(Policy(registry()), self.store)
        self.fields = {"restic-password": "original-restic-identity\n", "rclone.conf": "[GDrive]\ntype = drive\ntoken = original\n"}
        # Noncanonical whitespace and terminal newlines are part of the original envelope.
        self.original = json.dumps({"services": {"backrest": {"files": self.fields, "env": {}}}, "version": 1},
                                   ensure_ascii=False, separators=(", ", ":  ")).encode() + b"\n\n"
        self.broker.dispatch(0, request("initialize", "backrest", payload=base64.b64encode(self.original).decode()))

    def assert_denied(self, uid, message):
        with self.assertRaises(BrokerError) as error:
            self.broker.dispatch(uid, message)
        self.assertEqual(error.exception.code, "denied")

    def test_own_read_preserves_original_envelope_and_foreign_reads_are_denied(self):
        own = self.broker.dispatch(43002, request("read", "backrest"))
        self.assertEqual(base64.b64decode(own["payload"]), self.original)
        for component in ("vpn-ddns", "cloudflared", "nginx", "authelia", "authelia-oidc"):
            with self.subTest(component=component):
                self.assert_denied(43002, request("read", component))
        self.assert_denied(43022, request("read", "backrest"))
        exported = self.broker.dispatch(43022, request("export", "backrest"))
        ciphertext = base64.b64decode(exported["ciphertext"])
        self.assertTrue(ciphertext.startswith(b"age-encryption.org/v1\n"))
        self.assertNotIn(self.fields["restic-password"].encode(), ciphertext)
        self.assert_denied(43002, request("export", "backrest"))

    def test_only_the_exact_mutable_field_can_be_changed_by_a_service(self):
        updated = "[GDrive]\ntype = drive\ntoken = refreshed\n"
        self.broker.dispatch(43002, request("mutable", "backrest", field="rclone.conf", value=updated,
                                           expected_digest=digest(self.fields["rclone.conf"].encode())))
        fields = parse_payload("backrest", self.store.read("backrest"))
        self.assertEqual(fields, {**self.fields, "rclone.conf": updated})
        self.assert_denied(43002, request("mutable", "backrest", field="restic-password", value="replacement",
                                         expected_digest=digest(self.fields["restic-password"].encode())))
        self.assert_denied(43002, request("store", "backrest", payload=base64.b64encode(self.original).decode(),
                                         expected_digest=digest(self.store.read("backrest"))))
        self.assert_denied(43002, request("mutable", "authelia", field="users.yml", value="replacement",
                                         expected_digest="0" * 64))
        self.assertEqual(parse_payload("backrest", self.store.read("backrest")), fields)

    def test_stale_mutable_and_admin_updates_never_overwrite_a_newer_record(self):
        stale = digest(self.fields["rclone.conf"].encode())
        admin_fields = {**self.fields, "rclone.conf": "[GDrive]\ntype = drive\ntoken = administrative-edit\n"}
        replacement = payload_bytes("backrest", admin_fields)
        self.broker.dispatch(1000, request("store", "backrest", payload=base64.b64encode(replacement).decode(),
                                          expected_digest=digest(self.original)))
        for message in (
            request("mutable", "backrest", field="rclone.conf", value=self.fields["rclone.conf"], expected_digest=stale),
            request("store", "backrest", payload=base64.b64encode(self.original).decode(), expected_digest=digest(self.original)),
        ):
            with self.subTest(operation=message["operation"]), self.assertRaises(BrokerError) as error:
                self.broker.dispatch(1000, message)
            self.assertEqual(error.exception.code, "conflict")
            self.assertEqual(self.store.read("backrest"), replacement)

    def test_simultaneous_compare_and_swap_writes_have_exactly_one_winner(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def save(identity):
            encoded = payload_bytes("backrest", {**self.fields, "restic-password": identity})
            barrier.wait()
            try:
                self.broker.dispatch(1000, request("store", "backrest", payload=base64.b64encode(encoded).decode(),
                                                  expected_digest=digest(self.original)))
                outcomes.append(("saved", encoded))
            except BrokerError as error:
                outcomes.append((error.code, encoded))
        workers = [threading.Thread(target=save, args=(identity,)) for identity in ("first-admin-edit", "second-admin-edit")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(outcome for outcome, _ in outcomes), ["conflict", "saved"])
        winner = next(encoded for outcome, encoded in outcomes if outcome == "saved")
        self.assertEqual(self.store.read("backrest"), winner)

    def test_uncertain_readback_restores_exact_original_and_keeps_encrypted_backup(self):
        replacement = payload_bytes("backrest", {**self.fields, "restic-password": "new-admin-identity"})
        original_read = self.store.read
        failed = False
        def fail_committed_readback(component):
            nonlocal failed
            value = original_read(component)
            if value == replacement and not failed:
                failed = True
                raise OSError("PRIVATE_ERROR_DETAIL")
            return value
        with patch.object(self.store, "read", side_effect=fail_committed_readback):
            with self.assertRaises(BrokerError) as error:
                self.broker.dispatch(1000, request("store", "backrest", payload=base64.b64encode(replacement).decode(),
                                                  expected_digest=digest(self.original)))
        self.assertEqual(error.exception.code, "unavailable")
        self.assertEqual(self.store.read("backrest"), self.original)
        backups = list((self.store.directory / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.crypto.decrypt("backrest", backups[0].read_bytes()), self.original)
        for path in [self.store.directory / "backrest.cred", *backups]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(self.fields["restic-password"].encode(), path.read_bytes())

    def test_initialization_never_replays_a_stale_seed(self):
        result = self.broker.dispatch(0, request("initialize", "backrest", payload=base64.b64encode(b"old-invalid-seed").decode()))
        self.assertTrue(result["preserved"])
        self.assertEqual(self.store.read("backrest"), self.original)

    def test_restart_cleans_only_a_dead_owned_socket_not_live_or_unrelated_paths(self):
        path = self.root / "restart.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as live:
            live.bind(str(path))
            live.listen()
            inode = path.lstat().st_ino
            with self.assertRaises(BrokerError):
                remove_stale_socket(path)
            self.assertEqual(path.lstat().st_ino, inode)
        remove_stale_socket(path)
        self.assertFalse(path.exists())
        path.write_bytes(b"unrelated-private-file")
        with self.assertRaises(BrokerError):
            remove_stale_socket(path)
        self.assertEqual(path.read_bytes(), b"unrelated-private-file")
        alias = self.root / "alias.sock"
        alias.symlink_to(path)
        with self.assertRaises(BrokerError):
            remove_stale_socket(alias)
        self.assertTrue(alias.is_symlink())

    def test_systemd_receives_readiness_on_path_and_abstract_notify_sockets(self):
        for abstract in (False, True):
            with self.subTest(abstract=abstract), socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                address = str(self.root / "notify.sock")
                if abstract:
                    address = "\0homelab-test-" + self.root.name
                receiver.bind(address)
                receiver.settimeout(2)
                configured = "@" + address[1:] if abstract else address
                with patch.dict(os.environ, {"NOTIFY_SOCKET": configured}):
                    notify_ready()
                self.assertEqual(receiver.recv(4096), b"READY=1\n")

    @unittest.skipIf(os.getuid() == 0, "offline seed import must run as the configured operator")
    def test_ha_seed_keeps_mounted_runtime_and_separately_encrypts_unused_physical_secrets(self):
        runtime = self.root / "runtime"
        mounted = runtime / "homelab-secrets/homeassistant/secrets.yaml"
        physical = self.root / "physical/homeassistant/config/secrets.yaml"
        current = "smtp_password: original-smtp\nsome_password: original-other\n"
        unused = "some_password: unused-physical-copy\n"
        for source, contents in [(mounted, current), (physical, unused)]:
            source.parent.mkdir(parents=True)
            source.write_text(contents)
            source.chmod(0o600)
        original = json.dumps({"services": {"homeassistant": {"files": {"secrets.yaml": current}, "env": {}}},
                               "version": 1}, indent=2).encode() + b"\n\n"
        settings = {
            "operator": {"uid": os.getuid()},
            "physicalStateRoot": str(self.root / "physical"),
            "credentialSeedDirectory": str(self.root / "seeds"),
        }
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}), \
                patch("credential_broker.NativeCrypto", return_value=self.crypto), \
                patch("credential_broker.original_record", return_value=original):
            import_seed(settings, ["homeassistant"], None)
            active = self.crypto.seed_decrypt("homeassistant", (self.root / "seeds/homeassistant.cred").read_bytes())
            self.assertEqual(active, original)
            recovery = self.crypto.seed_decrypt("homeassistant",
                                               (self.root / "seeds/.homeassistant-physical.cred").read_bytes())
            self.assertEqual(parse_payload("homeassistant", recovery)["secrets.yaml"], unused)
            self.assertEqual(mounted.read_text(), current)
            self.assertEqual(physical.read_text(), unused)

    @unittest.skipIf(os.getuid() == 0, "offline seed import must run as the configured operator")
    def test_ha_seed_uses_live_private_yaml_not_stale_keyring_and_refuses_changed_seed(self):
        source = self.root / "secrets.yaml"
        current = "midea_password: current-original\n"
        source.write_text(current)
        source.chmod(0o600)
        stale = payload_bytes("homeassistant", {"secrets.yaml": "midea_password: stale-snapshot\n"})
        settings = {
            "operator": {"uid": os.getuid()},
            "physicalStateRoot": str(self.root),
            "credentialSeedDirectory": str(self.root / "seeds"),
        }
        with patch("credential_broker.NativeCrypto", return_value=self.crypto), \
                patch("credential_broker.original_record", return_value=stale):
            import_seed(settings, ["homeassistant"], str(source))
            target = self.root / "seeds/homeassistant.cred"
            original_seed = target.read_bytes()
            decrypted = self.crypto.seed_decrypt("homeassistant", original_seed)
            self.assertEqual(parse_payload("homeassistant", decrypted)["secrets.yaml"], current)
            source.write_text("midea_password: newer-original\n")
            with self.assertRaises(BrokerError) as error:
                import_seed(settings, ["homeassistant"], str(source))
            self.assertEqual(error.exception.code, "conflict")
            self.assertEqual(target.read_bytes(), original_seed)
            self.assertEqual(source.read_text(), "midea_password: newer-original\n")

    def test_socket_uses_real_peer_and_rejects_forged_claims_without_value_leakage(self):
        uid = 43002 if os.getuid() == 0 else os.getuid()
        settings = registry(backrest_uid=uid, operator_uid=os.getuid() + 100000)
        # Avoid accidental collisions when a test is run as an existing fixture role.
        for name, role in settings["principals"].items():
            if name != "backrest" and role["uid"] == uid:
                role["uid"] += 200000
                if name == "credentials":
                    settings["definitions"]["credentials"]["uid"] = role["uid"]
        path = self.root / "control.sock"
        self.root.chmod(0o755)
        server = CredentialServer(str(path), RequestHandler)
        server.broker = Broker(Policy(settings), self.store)
        path.chmod(0o666)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        messages = [request("read", "backrest"), request("read", "vpn-ddns"),
                    request("read", "cloudflared"), request("read", "nginx"),
                    request("read", "backrest", uid=0, identity="PRIVATE_FORGED_IDENTITY")]
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=peer_requests, args=(str(path), messages, uid, child))
        try:
            process.start()
            child.close()
            self.assertTrue(parent.poll(15), "credential peer did not return a result")
            results = parent.recv()
            process.join(15)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(base64.b64decode(results[0]["payload"]), self.original)
            for result in results[1:4]:
                self.assertEqual(result, {"ok": False, "error": "denied"})
            self.assertEqual(results[4], {"ok": False, "error": "invalid"})
            self.assertNotIn("PRIVATE_FORGED_IDENTITY", json.dumps(results))
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
            parent.close()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
