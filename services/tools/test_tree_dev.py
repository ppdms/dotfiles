import argparse
from contextlib import closing
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("tree_dev", Path(__file__).with_name("tree-dev.py"))
tree = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tree)


class BranchLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.prod, self.dev = root / "prod", root / "dev"
        (self.prod / "jobs").mkdir(parents=True)
        (self.prod / "objects").mkdir()
        self.dev.mkdir()
        (self.prod / "session").write_text("original-session\n")
        (self.prod / "code").write_text("original-code\n")
        (self.prod / "objects" / "original").write_bytes(b"original-object")
        self.database = self.prod / "jobs/sqlite.db"
        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute("CREATE TABLE tree_go_migrations (version integer)")
            database.execute("INSERT INTO tree_go_migrations VALUES (1)")
        self.original = self.database.read_bytes()
        for target, value in (("prod_state", self.prod), ("dev_state", self.dev)):
            started = patch.object(tree, target, return_value=value)
            started.start()
            self.addCleanup(started.stop)
        identity = patch.object(tree, "principal", return_value={"uid": os.getuid(), "gid": os.getgid()})
        identity.start()
        self.addCleanup(identity.stop)

    def test_database_branch_preserves_original_and_has_original_migration_ledger(self):
        destination = self.dev / "jobs/sqlite.db"
        tree.branch_database(self.database, destination)
        self.assertEqual(self.database.read_bytes(), self.original)
        with closing(sqlite3.connect(destination)) as database:
            self.assertEqual(database.execute("SELECT version FROM tree_go_migrations").fetchall(), [(1,)])
        self.assertEqual(destination.stat().st_uid, os.getuid())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_failed_dev_migration_restores_production_and_preserves_original_identity(self):
        with patch.object(tree, "checkout_root", return_value=self.prod), \
             patch.object(tree, "git_rev", return_value="a" * 40), \
             patch.object(tree, "active", return_value=False), \
             patch.object(tree, "image_exists", return_value=True), \
             patch.object(tree, "stop_prod", return_value=True), \
             patch.object(tree, "start_prod") as restore, \
             patch.object(tree, "systemctl", return_value=Mock(returncode=0)), \
             patch.object(tree, "run_oneshot", side_effect=tree.DevError("migration failed")), \
             patch.object(tree, "set_tree_route") as route:
            with self.assertRaisesRegex(tree.DevError, "migration failed"):
                tree.cmd_up(argparse.Namespace())
            restore.assert_called_once_with()
            route.assert_not_called()
        self.assertEqual(self.database.read_bytes(), self.original)
        self.assertEqual((self.dev / "session").read_text(), "original-session\n")
        self.assertEqual((self.dev / "code").read_text(), "original-code\n")
        self.assertEqual((self.prod / "objects/original").read_bytes(), b"original-object")

    def test_down_discards_only_dev_branch_not_production_or_original_identity(self):
        tree.branch_database(self.database, self.dev / "jobs/sqlite.db")
        tree.sync_objects(self.prod / "objects", self.dev / "objects")
        (self.dev / "session").write_text("original-session\n")
        (self.dev / "code").write_text("original-code\n")
        (self.dev / "prod-was-running").write_text("0\n")
        (self.dev / "branch").write_text("disposable\n")
        with patch.object(tree, "active", return_value=False), \
             patch.object(tree, "systemctl", return_value=Mock(returncode=0)), \
             patch.object(tree, "set_tree_route") as route, \
             patch.object(tree, "start_prod") as restore:
            tree.cmd_down(argparse.Namespace())
            route.assert_called_once_with(False)
            restore.assert_not_called()
        self.assertFalse((self.dev / "jobs/sqlite.db").exists())
        self.assertEqual(list((self.dev / "objects").iterdir()), [])
        self.assertEqual(self.database.read_bytes(), self.original)
        self.assertEqual((self.prod / "objects/original").read_bytes(), b"original-object")
        self.assertEqual((self.dev / "session").read_text(), "original-session\n")
        self.assertEqual((self.dev / "code").read_text(), "original-code\n")

    def test_dev_symlink_cannot_authorize_production_database_deletion(self):
        (self.dev / "jobs").symlink_to(self.prod / "jobs", target_is_directory=True)
        with patch.object(tree, "active", return_value=False), \
             patch.object(tree, "systemctl", return_value=Mock(returncode=0)), \
             patch.object(tree, "set_tree_route"):
            with self.assertRaisesRegex(tree.DevError, "symlink"):
                tree.cmd_down(argparse.Namespace())
        self.assertEqual(self.database.read_bytes(), self.original)


class ProductionPromotionTests(unittest.TestCase):
    def test_failed_candidate_health_restores_original_image_not_candidate(self):
        runtime = []
        labels = {"org.opencontainers.image.revision": "a" * 40}
        def podman(*args, **kwargs):
            runtime.append((args, kwargs.get("role", "tree-dev")))
            if args[:2] == ("image", "inspect"):
                return Mock(returncode=0, stdout='{"org.opencontainers.image.revision": "' + "a" * 40 + '"}' if args[-1] == "sha256:new" else "sha256:original", stderr="")
            return Mock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(tree, "checkout_root", return_value=Path(temporary)), \
             patch.object(tree, "prod_state", return_value=Path(temporary) / "prod"), \
             patch.object(tree, "git_rev", return_value="a" * 40), \
             patch.object(tree, "active", return_value=False), \
             patch.object(tree, "tree_inputs", return_value=({}, Path(temporary) / "pinned")), \
             patch.object(tree, "production_candidate", return_value="sha256:new"), \
             patch.object(tree, "stream_image", return_value="sha256:new"), \
             patch.object(tree, "inspect_labels", return_value=labels), \
             patch.object(tree, "podman", side_effect=podman), \
             patch.object(tree, "stop_prod", return_value=True), \
             patch.object(tree, "systemctl", return_value=Mock(returncode=0)), \
             patch.object(tree, "start_prod") as start, \
             patch.object(tree, "wait_health", side_effect=tree.DevError("candidate unhealthy")):
            with self.assertRaisesRegex(tree.DevError, "candidate unhealthy"):
                tree.cmd_deploy(argparse.Namespace())
        self.assertIn((("tag", "sha256:original", "localhost/tree-eclass:stable"), "tree-eclass"), runtime)
        self.assertEqual(start.call_count, 2)
        migration = next((args, role) for args, role in runtime if args[0] == "run")
        self.assertEqual(migration[1], "tree-eclass")
        self.assertIn("keep-id:uid=10001,gid=10001", migration[0])


class ApprovedBaseTests(unittest.TestCase):
    def test_failed_acquisition_does_not_transfer_partial_base(self):
        reference = "docker.io/example/base@sha256:" + "1" * 64
        with patch.dict(os.environ, {"HOMELAB_SIGNATURE_POLICY": "/policy.json"}), \
             patch.object(tree, "podman", return_value=Mock(returncode=1)), \
             patch.object(tree, "run_as", return_value=Mock(returncode=1, stderr="invalid registry image archive")), \
             patch.object(tree, "stream_image") as stream:
            with self.assertRaisesRegex(tree.DevError, "invalid registry image archive"):
                tree.acquire_base(reference)
            stream.assert_not_called()

    def test_unavailable_runtime_does_not_trigger_acquisition(self):
        reference = "docker.io/example/base@sha256:" + "1" * 64
        with patch.object(tree, "podman", return_value=Mock(returncode=125)), \
             patch.object(tree, "run_as") as acquire, \
             patch.object(tree, "stream_image") as stream:
            with self.assertRaisesRegex(tree.DevError, "store is unavailable"):
                tree.acquire_base(reference)
            acquire.assert_not_called()
            stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
