import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


secrets = load("backrest_management_test_secrets", "homelab-secrets.py")
with patch.dict(sys.modules, {"homelab_secrets": secrets}):
    provision = load("backrest_provision", "backrest-provision.py")
repository = load("backrest_repository", "backrest-repository.py")


class ProvisionPreservationTests(unittest.TestCase):
    def test_original_sqlite_copy_is_installed_in_authelia_account_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original.sqlite3"
            with closing(sqlite3.connect(source)) as database, database:
                database.execute("CREATE TABLE identity (value text)")
                database.execute("INSERT INTO identity VALUES ('original')")
            original = source.read_bytes()
            calls = []
            def dispatch(role, argv, **kwargs):
                calls.append(role)
                return subprocess.run(argv, **kwargs)
            with patch.object(provision, "principal", return_value={"stateRoot": str(root / "authelia-view")}), \
                 patch.object(provision, "tool", return_value=sys.executable), \
                 patch.object(provision, "run_as", side_effect=dispatch):
                provision.install_database(source)
                destination = root / "authelia-view/authelia/data/db.sqlite3"
                with closing(sqlite3.connect(destination)) as database, database:
                    self.assertEqual(database.execute("SELECT value FROM identity").fetchone(), ('original',))
                    database.execute("UPDATE identity SET value='already-live'")
                provision.install_database(source)
                with closing(sqlite3.connect(destination)) as database:
                    self.assertEqual(database.execute("SELECT value FROM identity").fetchone(), ('already-live',))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(calls, ["authelia", "authelia"])
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_replaying_recipe_does_not_replace_refreshed_encrypted_field(self):
        with tempfile.TemporaryDirectory() as directory:
            recipe = Path(directory) / "inputs.json"
            recipe.write_text(json.dumps({"resticPasswordFile": str(Path(directory) / "missing-stale-source")}))
            with patch.object(sys, "argv", ["provision", "--inputs", str(recipe), "--no-start"]), \
                 patch.object(provision, "require_admin"), \
                 patch.object(provision, "database_exists", return_value=True), \
                 patch.object(provision, "load_field", return_value=b"current-encrypted-field"), \
                 patch.object(provision, "load_record", return_value=({"restic-password": "current-encrypted-field"}, "current-digest")), \
                 patch.object(provision, "store_fields") as store, \
                 patch.object(provision, "manifest", return_value={"principals": {"backrest": {"components": ["backrest"]}}}), \
                 patch.object(provision, "run_as", return_value=Mock(returncode=0)), \
                 patch.dict(os.environ, {"HOMELAB_SECRETS": "/packaged/homelab-secrets"}):
                self.assertEqual(provision.main(), 0)
                store.assert_not_called()

    def test_invalid_new_input_prevents_all_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            password, rclone = root / "password", root / "rclone"
            password.write_text("fixture-password")
            rclone.write_text("not an rclone configuration")
            for path in (password, rclone):
                path.chmod(0o600)
            recipe = root / "inputs.json"
            recipe.write_text(json.dumps({"resticPasswordFile": str(password), "rcloneConfigFile": str(rclone)}))
            with patch.object(sys, "argv", ["provision", "--inputs", str(recipe), "--no-start"]), \
                 patch.object(provision, "require_admin"), \
                 patch.object(provision, "database_exists", return_value=True), \
                 patch.object(provision, "load_field", return_value=None), \
                 patch.object(provision, "store_fields") as store:
                with self.assertRaisesRegex(ValueError, "rclone.conf"):
                    provision.main()
                store.assert_not_called()


class RepositoryAuthorityTests(unittest.TestCase):
    def test_existing_repository_selection_mismatch_is_not_replaced_or_indexed(self):
        guid = "a" * 64
        with patch.object(repository, "principal", return_value={"uid": os.getuid()}), \
             patch.object(repository, "run_as", return_value=Mock(returncode=0)), \
             patch.object(repository, "restic", side_effect=[{"id": guid}, []]), \
             patch.object(repository, "rpc", return_value={"repos": [{"id": "original", "guid": "b" * 64, "uri": "rclone:GDrive:original"}]}) as rpc, \
             patch.dict(os.environ, {"HOMELAB_REPOSITORY_URI": "rclone:GDrive:original", "HOMELAB_REPOSITORY_ID": "original", "HOMELAB_SECRETS": "/packaged/homelab-secrets"}):
            with self.assertRaises(RuntimeError):
                repository.main()
            self.assertEqual([call.args[0] for call in rpc.call_args_list], ["GetConfig"])


if __name__ == "__main__":
    unittest.main()
