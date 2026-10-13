from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


secrets = load_module("secrets_export_test_secrets", "homelab-secrets.py")
with patch.dict(sys.modules, {"homelab_secrets": secrets}):
    exporter = load_module("secrets_export_test_exporter", "secrets-export.py")


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "age tools required")
class SecretExportRecoveryTests(unittest.TestCase):
    def test_partial_broker_or_write_failures_retain_previous_recoverable_ciphertext(self):
        for failure in ("broker", "write"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                identity = root / "identity"
                subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
                recipient = subprocess.run(["age-keygen", "-y", str(identity)], check=True,
                                           capture_output=True, text=True).stdout.strip()
                directory = root / "secrets-export"
                directory.mkdir(mode=0o700)
                previous = directory / "nginx.json.age"
                subprocess.run(["age", "-r", recipient, "-o", str(previous)],
                               input=b"previous-recoverable-credential", check=True, capture_output=True)
                previous.chmod(0o600)
                previous_ciphertext = previous.read_bytes()
                healthy = {"restic-password": "fixture-backup-password", "rclone.conf": "[GDrive]\ntype = drive\n"}
                payload = secrets.payload_bytes("backrest", healthy)
                healthy_ciphertext = subprocess.run(["age", "-r", recipient], input=payload,
                                                    check=True, capture_output=True).stdout

                def records(component):
                    if component == "backrest":
                        return healthy_ciphertext
                    if component == "nginx":
                        if failure == "broker":
                            raise RuntimeError("PRIVATE_ERROR_DETAIL")
                        return healthy_ciphertext
                    return None

                native_write = exporter.atomic_write
                def write(path, value):
                    if failure == "write" and path.name == "nginx.json.age":
                        raise OSError("PRIVATE_ERROR_DETAIL")
                    native_write(path, value)

                diagnostics = io.StringIO()
                with patch.dict(os.environ, {"HOMELAB_STATE_ROOT": str(root)}), \
                        patch.object(exporter, "manifest", return_value={"principals": {}}), \
                        patch.object(exporter, "export_record", side_effect=records), \
                        patch.object(exporter, "atomic_write", side_effect=write), \
                        redirect_stdout(io.StringIO()), redirect_stderr(diagnostics):
                    self.assertEqual(exporter.main(), 1)
                self.assertEqual(previous.read_bytes(), previous_ciphertext)
                self.assertEqual(sorted(path.name for path in directory.iterdir()), ["backrest.json.age", "nginx.json.age"])
                self.assertNotIn("PRIVATE_ERROR_DETAIL", diagnostics.getvalue())
                decrypted = subprocess.run(["age", "-d", "-i", str(identity), str(directory / "backrest.json.age")],
                                           check=True, capture_output=True).stdout
                self.assertEqual(decrypted, payload)
                self.assertEqual(json.loads(decrypted)["services"]["backrest"]["files"], healthy)
                self.assertEqual((directory / "backrest.json.age").stat().st_mode & 0o777, 0o600)


class CiphertextBackupAccessTests(unittest.TestCase):
    def test_failed_backup_acl_preserves_the_previous_ciphertext(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "backrest.json.age"
            original = b"age-encryption.org/v1\nprevious-fixture-ciphertext"
            target.write_bytes(original)
            target.chmod(0o600)
            with patch.object(exporter.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaises(RuntimeError):
                    exporter.publish_ciphertext(target, b"age-encryption.org/v1\nreplacement-fixture-ciphertext", 43002)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual([path.name for path in directory.iterdir()], [target.name])

    @unittest.skipUnless(shutil.which("setfacl") and shutil.which("getfacl"), "native ACL tools required")
    def test_ciphertext_is_readable_to_named_backup_uid_but_not_owning_group_or_world(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"HOMELAB_SETFACL": shutil.which("setfacl")}):
            target = Path(temporary) / "backrest.json.age"
            uid = 43002 if os.getuid() != 43002 else 43022
            value = b"age-encryption.org/v1\nfixture-ciphertext"
            exporter.publish_ciphertext(target, value, uid)
            acl = subprocess.run(["getfacl", "--numeric", "--omit-header", str(target)],
                                 check=True, capture_output=True, text=True).stdout
            self.assertIn("user:" + str(uid) + ":r--", acl)
            self.assertIn("group::---", acl)
            self.assertIn("other::---", acl)
            self.assertIn("mask::r--", acl)
            self.assertEqual(target.read_bytes(), value)
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
