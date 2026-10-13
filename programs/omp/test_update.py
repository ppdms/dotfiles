import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("omp_update", Path(__file__).with_name("update.py"))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)

OLD_HASH = "sha256-" + base64.b64encode(bytes(32)).decode()
NEW_HASH = "sha256-" + base64.b64encode(bytes([1]) * 32).decode()


class UpdateTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "checkout"
        (self.checkout / ".git").mkdir(parents=True)
        (self.checkout / "flake.nix").write_text("{}\n")
        (self.checkout / "tools").mkdir()
        self.pin = self.checkout / "tools" / "omp-source.json"
        self.original = json.dumps({"version": "18.5.1", "hash": OLD_HASH, "note": "keep"}).encode() + b"\n"
        self.pin.write_bytes(self.original)
        self.pin.chmod(0o640)
        self.old_package = self.root / "old-package"
        self.old_package.mkdir()
        self.profile = self.root / "profile"
        self.profile.symlink_to(self.old_package)

    def invoke(self):
        with mock.patch.dict(os.environ, {"HOMELAB_NIX": "nix", "HOMELAB_NIX_ENV": "nix-env"}), \
             mock.patch.object(updater, "latest_release", return_value=("18.6.0", "https://example.invalid/asset", None)), \
             mock.patch.object(updater, "prefetch_release", return_value=NEW_HASH), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return updater.main(["--checkout", str(self.checkout), "--profile", str(self.profile)])

    def assert_previous_installation(self):
        self.assertEqual(self.profile.resolve(), self.old_package)

    def test_build_failure_restores_pin_and_preserves_profile(self):
        failure = subprocess.CalledProcessError(1, ["nix", "build"])
        with mock.patch.object(updater, "build_package", side_effect=failure):
            self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.pin.read_bytes(), self.original)
        self.assertEqual(self.pin.stat().st_mode & 0o777, 0o640)
        self.assert_previous_installation()

    def test_profile_install_failure_restores_pin_and_preserves_profile(self):
        failure = subprocess.CalledProcessError(1, ["nix-env", "--set"])
        with mock.patch.object(updater, "build_package", return_value=str(self.root / "built-package")), \
             mock.patch.object(updater.subprocess, "run", side_effect=failure):
            self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.pin.read_bytes(), self.original)
        self.assert_previous_installation()

    def test_independent_pin_edit_is_not_overwritten_or_installed(self):
        independent = json.dumps({"version": "18.7.0", "hash": OLD_HASH}).encode()

        def edit_during_build(*arguments):
            self.pin.write_bytes(independent)
            return str(self.root / "built-package")

        with mock.patch.object(updater, "build_package", side_effect=edit_during_build):
            self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.pin.read_bytes(), independent)
        self.assert_previous_installation()

    def test_newer_numeric_pin_is_not_downgraded(self):
        newer = json.dumps({"version": "18.10.0", "hash": OLD_HASH}).encode()
        self.pin.write_bytes(newer)
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.pin.read_bytes(), newer)
        self.assert_previous_installation()

    def test_prefetch_digest_mismatch_is_rejected_before_installation(self):
        prefetch = json.dumps({"hash": NEW_HASH, "storePath": "/nix/store/test-source"})
        with mock.patch.object(updater, "command_output", return_value=prefetch):
            with self.assertRaises(updater.UpdateError):
                updater.prefetch_release("nix", "https://example.invalid/asset", bytes(32))


if __name__ == "__main__":
    unittest.main()
