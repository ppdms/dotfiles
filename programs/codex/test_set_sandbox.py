"""Explicit Codex policy and atomic private-file boundaries."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tomlkit


spec = importlib.util.spec_from_file_location(
    "codex_config", Path(__file__).with_name("set-sandbox.py"))
codex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codex)


class CodexConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.target = self.home / ".codex/config.toml"
        self.original = (
            '# Keep this root comment\n'
            'sandbox_mode = "danger-full-access" # Keep this policy comment\n'
            'model = "example-model"\n'
            '\n'
            '[profiles.work] # Keep this profile comment\n'
            'sandbox_mode = "workspace-write"\n'
            'model = "profile-model"\n'
        ).encode()

    def existing_config(self, content=None):
        self.target.parent.mkdir()
        self.target.write_bytes(self.original if content is None else content)
        self.target.chmod(0o640)

    def test_omitted_and_null_policy_never_create_directory(self):
        codex.set_sandbox(self.target)
        codex.set_sandbox(self.target, None)
        self.assertFalse(self.target.parent.exists())

    def test_unmanaged_existing_full_access_file_is_byte_identical(self):
        self.existing_config()
        before = self.target.stat()
        codex.set_sandbox(self.target)
        codex.set_sandbox(self.target, None)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(self.target.stat().st_ino, before.st_ino)
        self.assertEqual(self.target.stat().st_mode, before.st_mode)

    def test_each_explicit_mode_preserves_comments_settings_and_profiles(self):
        self.existing_config()
        for mode in ("read-only", "workspace-write", "danger-full-access"):
            with self.subTest(mode=mode):
                self.target.write_bytes(self.original)
                codex.set_sandbox(self.target, mode)
                expected = self.original.replace(
                    b'sandbox_mode = "danger-full-access"',
                    f'sandbox_mode = "{mode}"'.encode(), 1)
                self.assertEqual(self.target.read_bytes(), expected)
                self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
                self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_missing_root_key_is_added_without_changing_named_profile(self):
        source = b'# Root comment\nmodel = "example"\n[profiles.work]\nsandbox_mode = "read-only"\n'
        self.existing_config(source)
        codex.set_sandbox(self.target, "workspace-write")
        result = self.target.read_text()
        document = tomlkit.parse(result)
        self.assertEqual(document["sandbox_mode"], "workspace-write")
        self.assertEqual(document["profiles"]["work"]["sandbox_mode"], "read-only")
        self.assertIn('# Root comment\nmodel = "example"\n', result)
        self.assertIn('[profiles.work]\nsandbox_mode = "read-only"\n', result)

    def test_new_config_is_private_for_each_explicit_mode(self):
        for mode in ("read-only", "workspace-write", "danger-full-access"):
            with self.subTest(mode=mode):
                target = self.home / mode / "config.toml"
                codex.set_sandbox(target, mode)
                self.assertEqual(tomlkit.parse(target.read_text())["sandbox_mode"], mode)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_invalid_policy_never_creates_or_rewrites_config(self):
        for mode in ("full-access", "", True, 1, {}, []):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    codex.set_sandbox(self.target, mode)
                self.assertFalse(self.target.parent.exists())
        self.existing_config()
        with self.assertRaises(ValueError):
            codex.set_sandbox(self.target, "full-access")
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)

    def test_malformed_toml_and_encoding_remain_unchanged_without_leaking(self):
        self.existing_config()
        for content in (b'private_marker = "DO-NOT-DISPLAY-SECRET\n', b'\xff'):
            with self.subTest(content=content):
                self.target.write_bytes(content)
                errors = io.StringIO()
                with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                     contextlib.redirect_stderr(errors):
                    self.assertEqual(codex.main(["read-only"]), 1)
                self.assertEqual(self.target.read_bytes(), content)
                self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)
                self.assertNotIn("DO-NOT-DISPLAY-SECRET", errors.getvalue())
                self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_failed_atomic_replace_retains_original_and_removes_temporary(self):
        self.existing_config()
        with mock.patch.object(codex.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                codex.set_sandbox(self.target, "read-only")
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])


if __name__ == "__main__":
    unittest.main()
