import argparse
from contextlib import closing
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(os.environ.get("HOMELAB_TEST_TOOLS", str(Path(__file__).parent)))
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location("tree_dev", TOOLS / "tree-dev.py")
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
        with (
            patch.object(tree, "checkout_root", return_value=self.prod),
            patch.object(tree, "git_rev", return_value="a" * 40),
            patch.object(tree, "active", return_value=False),
            patch.object(tree, "image_exists", return_value=True),
            patch.object(tree, "systemctl", return_value=Mock(returncode=0)),
            patch.object(tree, "run_oneshot", side_effect=tree.DevError("migration failed")),
            patch.object(tree, "clear_tree_route") as route,
        ):
            with self.assertRaisesRegex(tree.DevError, "migration failed"):
                tree.cmd_up(argparse.Namespace())
            route.assert_not_called()
        # The failed branch is discarded; the prod database is untouched and
        # the copied identity is removed with the branch.
        self.assertEqual(self.database.read_bytes(), self.original)
        self.assertFalse((self.dev / "session").exists())
        self.assertFalse((self.dev / "code").exists())
        self.assertFalse((self.dev / "jobs/sqlite.db").exists())
        self.assertEqual((self.prod / "objects/original").read_bytes(), b"original-object")

    def test_objects_branch_shares_inodes_and_survives_branch_wipe(self):
        tree.sync_objects(self.prod / "objects", self.dev / "objects")
        branched = self.dev / "objects/original"
        self.assertEqual(branched.read_bytes(), b"original-object")
        try:
            same_inode = branched.stat().st_ino == (self.prod / "objects/original").stat().st_ino
        except OSError:
            same_inode = False
        if same_inode:
            # A staging write lands on a new inode; prod bytes are unchanged.
            branched.unlink()
            branched.write_bytes(b"staging-write")
            self.assertEqual((self.prod / "objects/original").read_bytes(), b"original-object")
            self.assertNotEqual(branched.stat().st_ino, (self.prod / "objects/original").stat().st_ino)


    def test_failed_sync_migration_restores_previous_branch(self):
        tree.branch_database(self.database, self.dev / "jobs/sqlite.db")
        tree.sync_objects(self.prod / "objects", self.dev / "objects")
        (self.dev / "branch").write_text("previous-branch\n")
        (self.dev / "rev").write_text("b" * 40 + "\n")
        calls = {"migrated": 0}

        def migrate(_unit):
            calls["migrated"] += 1
            if calls["migrated"] == 1:
                raise tree.DevError("migration failed")

        with (
            patch.object(tree, "checkout_root", return_value=self.prod),
            patch.object(tree, "git_rev", return_value="c" * 40),
            patch.object(tree, "active", return_value=True),
            patch.object(tree, "image_exists", return_value=True),
            patch.object(tree, "systemctl", return_value=Mock(returncode=0)),
            patch.object(tree, "run_oneshot", side_effect=migrate),
            patch.object(tree, "stop_dev_stack", return_value=True),
            patch.object(tree, "start_dev_stack"),
            patch.object(tree, "clear_tree_route"),
            patch.object(tree, "build_image"),
            patch.object(tree, "podman", return_value=Mock(returncode=0)),
        ):
            with self.assertRaisesRegex(tree.DevError, "previous code"):
                tree.cmd_sync(argparse.Namespace())
            # Two migrations ran: the failed new branch, then the restored one.
            self.assertEqual(calls["migrated"], 2)
        # The restored branch is a fresh prod copy, not the failed migration.
        with closing(sqlite3.connect(self.dev / "jobs/sqlite.db")) as database:
            self.assertEqual(database.execute("SELECT version FROM tree_go_migrations").fetchall(), [(1,)])
        self.assertEqual(self.database.read_bytes(), self.original)

    def test_down_discards_only_dev_branch_not_production_or_original_identity(self):
        tree.branch_database(self.database, self.dev / "jobs/sqlite.db")
        tree.sync_objects(self.prod / "objects", self.dev / "objects")
        (self.dev / "session").write_text("original-session\n")
        (self.dev / "code").write_text("original-code\n")
        (self.dev / "branch").write_text("disposable\n")
        with (
            patch.object(tree, "active", return_value=False),
            patch.object(tree, "systemctl", return_value=Mock(returncode=0)),
            patch.object(tree, "clear_tree_route") as route,
        ):
            tree.cmd_down(argparse.Namespace())
            route.assert_called_once_with()
        self.assertFalse((self.dev / "jobs/sqlite.db").exists())
        self.assertEqual(list((self.dev / "objects").iterdir()), [])
        self.assertEqual(self.database.read_bytes(), self.original)
        self.assertEqual((self.prod / "objects/original").read_bytes(), b"original-object")
        # Identity files are part of the branch and discarded with it.
        self.assertFalse((self.dev / "session").exists())
        self.assertFalse((self.dev / "code").exists())

    def test_dev_symlink_cannot_authorize_production_database_deletion(self):
        (self.dev / "jobs").symlink_to(self.prod / "jobs", target_is_directory=True)
        with (
            patch.object(tree, "active", return_value=False),
            patch.object(tree, "systemctl", return_value=Mock(returncode=0)),
            patch.object(tree, "clear_tree_route"),
        ):
            with self.assertRaisesRegex(tree.DevError, "symlink"):
                tree.cmd_down(argparse.Namespace())
        self.assertEqual(self.database.read_bytes(), self.original)


class ApprovedBaseTests(unittest.TestCase):
    def test_failed_acquisition_does_not_transfer_partial_base(self):
        reference = "docker.io/example/base@sha256:" + "1" * 64
        with (
            patch.dict(os.environ, {"HOMELAB_SIGNATURE_POLICY": "/policy.json"}),
            patch.object(tree, "podman", return_value=Mock(returncode=1)),
            patch.object(tree, "run_as", return_value=Mock(returncode=1, stderr="invalid registry image archive")),
            patch.object(tree, "stream_image") as stream,
        ):
            with self.assertRaisesRegex(tree.DevError, "invalid registry image archive"):
                tree.acquire_base(reference)
            stream.assert_not_called()

    def test_unavailable_runtime_does_not_trigger_acquisition(self):
        reference = "docker.io/example/base@sha256:" + "1" * 64
        with (
            patch.object(tree, "podman", return_value=Mock(returncode=125)),
            patch.object(tree, "run_as") as acquire,
            patch.object(tree, "stream_image") as stream,
        ):
            with self.assertRaisesRegex(tree.DevError, "store is unavailable"):
                tree.acquire_base(reference)
            acquire.assert_not_called()
            stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
