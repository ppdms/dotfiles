from contextlib import closing
import errno
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(os.environ.get("HOMELAB_TEST_TOOLS", Path(__file__).parent))
sys.path.insert(0, str(TOOLS))
import tree_recovery as recovery


DATASET = ("jobs", "objects", "session", "code")


def file_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOATIME", 0))
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def contents(root):
    """Compare real bytes, ownership, modes, timestamps and extended attributes."""
    result = {}

    def visit(path):
        info = path.stat()
        metadata = (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, info.st_atime_ns, info.st_mtime_ns)
        attributes = {name: os.getxattr(path, name) for name in os.listxattr(path)}
        if path.is_dir():
            result[str(path.relative_to(root))] = (metadata, attributes, None)
            with recovery._directory(path) as descriptor:
                for name in sorted(os.listdir(descriptor)):
                    visit(path / name)
        else:
            result[str(path.relative_to(root))] = (metadata, attributes, file_bytes(path))

    for name in DATASET:
        if (root / name).exists():
            visit(root / name)
    return result


class RecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        (self.state / "jobs").mkdir(parents=True, mode=0o750)
        (self.state / "objects/nested").mkdir(parents=True)
        self.database = self.state / "jobs/sqlite.db"
        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute("CREATE TABLE tree_go_migrations (version integer)")
            database.execute("INSERT INTO tree_go_migrations VALUES (1)")
            database.execute("CREATE TABLE user_data (value text)")
            database.execute("INSERT INTO user_data VALUES ('original user data')")
        (self.state / "jobs/application.json").write_text('{"private":"original"}\n')
        (self.state / "objects/original").write_bytes(b"original-object")
        (self.state / "objects/nested/removed").write_bytes(b"keep-me")
        (self.state / "session").write_text("original-session\n")
        (self.state / "code").write_text("original-code\n")
        for parent in (self.state / "jobs", self.state / "objects", self.state / "objects/nested"):
            parent.chmod(0o750)
        for path in (self.database, self.state / "jobs/application.json", self.state / "session", self.state / "code"):
            path.chmod(0o600)
        for path in (self.state / "objects/original", self.state / "objects/nested/removed"):
            path.chmod(0o640)
        for path in self.state.rglob("*"):
            os.utime(path, ns=(1600000000123456789, 1600000000987654321))
        self.store = recovery.RecoveryStore(self.root / "recovery")

    def begin(self):
        return self.store.begin(self.state, "sha256:original", "sha256:candidate", True)

    def migrate(self):
        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute("ALTER TABLE user_data ADD COLUMN candidate_only text")
            database.execute("UPDATE user_data SET value='candidate user data', candidate_only='migrated'")
            database.execute("UPDATE tree_go_migrations SET version=2")
        (self.state / "jobs/application.json").write_text('{"private":"candidate"}\n')
        (self.state / "jobs/new-setting").write_bytes(b"candidate setting")
        # Immutable-object publishers replace paths rather than modifying inodes.
        replacement = self.state / "objects/.replacement"
        replacement.write_bytes(b"changed-object")
        os.replace(replacement, self.state / "objects/original")
        (self.state / "objects/nested/removed").unlink()
        (self.state / "objects/added").write_bytes(b"candidate-object")
        (self.state / "session").write_text("candidate-session\n")
        (self.state / "code").unlink()

    def assert_sqlite(self, root, version, value, migrated=False):
        with closing(sqlite3.connect(root / "jobs/sqlite.db")) as database:
            self.assertEqual(database.execute("SELECT version FROM tree_go_migrations").fetchall(), [(version,)])
            self.assertEqual(database.execute("SELECT value FROM user_data").fetchall(), [(value,)])
            columns = [row[1] for row in database.execute("PRAGMA table_info(user_data)")]
            self.assertEqual("candidate_only" in columns, migrated)

    def test_migration_permission_requires_checkpoint_and_is_revoked_before_recovery_or_admission(self):
        permit = self.store.directory / "migration.env"
        candidate = "sha256:" + "a" * 64
        with self.store.lock():
            record = self.store.begin(self.state, "sha256:original", candidate, True)
            with self.assertRaises(recovery.RecoveryError):
                self.store.allow_migration(record)
            self.assertFalse(permit.exists())
            self.store.checkpoint(record)
            self.store.allow_migration(record)
            self.assertEqual(permit.read_text(), f"TREE_RELEASE_IMAGE={candidate}\n")
            self.assertEqual(stat.S_IMODE(permit.stat().st_mode), 0o644)
            self.store.block(record)
            self.assertFalse(permit.exists())
            self.store.allow_migration(record)
            self.store.admit(record)
            self.assertFalse(permit.exists())
            with self.assertRaises(recovery.RecoveryError):
                self.store.allow_migration(record)

    def test_migration_permission_rejects_mutable_image_reference(self):
        with self.store.lock():
            record = self.store.begin(self.state, "sha256:original", "localhost/tree-eclass:stable", True)
            self.store.checkpoint(record)
            with self.assertRaises(recovery.RecoveryError):
                self.store.allow_migration(record)
            self.assertFalse((self.store.directory / "migration.env").exists())
            self.assertTrue((self.store.directory / "blocked").exists())

    def test_sqlite_migration_settings_objects_and_identity_are_exactly_reversible(self):
        original = contents(self.state)
        with self.store.lock():
            record = self.begin()
            self.assertEqual(record["phase"], "begun")
            self.assertTrue((self.store.directory / "blocked").exists())
            snapshot = self.store.checkpoint(record)
            self.assertEqual(contents(snapshot), original)
            self.assertEqual(record["phase"], "ready")
            for relative in ("jobs/sqlite.db", "jobs/application.json", "session", "code"):
                self.assertNotEqual((snapshot / relative).stat().st_ino, (self.state / relative).stat().st_ino)
            self.assertEqual(
                (snapshot / "objects/original").stat().st_ino, (self.state / "objects/original").stat().st_ino
            )
            self.migrate()
            failed = contents(self.state)
            self.assertEqual(contents(snapshot), original)
            rescue = self.store.restore(record)
            self.assertEqual(record["phase"], "restored")
            self.assertEqual(contents(self.state), original)
            self.assertEqual(contents(rescue), failed)
            self.assertEqual(
                (snapshot / "objects/original").stat().st_ino, (self.state / "objects/original").stat().st_ino
            )
            self.assert_sqlite(self.state, 1, "original user data")
            self.assert_sqlite(rescue, 2, "candidate user data", migrated=True)
            self.store.admit(record)
            self.assertFalse((self.store.directory / "blocked").exists())
            self.assertIsNotNone(self.store.pending())
            self.store.complete(record, "restored")
            self.assertIsNone(self.store.pending())
            self.assertTrue(snapshot.is_dir())
            self.assertTrue(rescue.is_dir())
            history = json.loads((snapshot.parent / "record.json").read_text())
            self.assertEqual(history["outcome"], "restored")
            self.assertEqual(history["rescue"], str(rescue))

    def test_immutable_objects_share_inodes_without_metadata_writes(self):
        original = contents(self.state)
        copy_metadata = recovery._copy_metadata
        objects = self.state / "objects"

        def protect_object_metadata(source, destination, info):
            if objects in source.parents and stat.S_ISREG(info.st_mode):
                self.fail("hardlinked object metadata must never be rewritten")
            copy_metadata(source, destination, info)

        with self.store.lock():
            record = self.begin()
            with patch.object(recovery, "_copy_metadata", side_effect=protect_object_metadata):
                snapshot = self.store.checkpoint(record)
            self.assertEqual(contents(self.state), original)
            self.assertEqual(contents(snapshot), original)
            for relative in ("objects/original", "objects/nested/removed"):
                self.assertEqual((snapshot / relative).stat().st_ino, (self.state / relative).stat().st_ino)
            for relative in ("jobs/sqlite.db", "jobs/application.json", "session", "code"):
                self.assertNotEqual((snapshot / relative).stat().st_ino, (self.state / relative).stat().st_ino)

    def test_cross_device_objects_fall_back_to_independent_exact_copies(self):
        original = contents(self.state)
        with self.store.lock():
            record = self.begin()
            with patch.object(recovery.os, "link", side_effect=OSError(errno.EXDEV, "different filesystems")):
                snapshot = self.store.checkpoint(record)
            self.assertEqual(contents(self.state), original)
            self.assertEqual(contents(snapshot), original)
            for relative in ("objects/original", "objects/nested/removed"):
                self.assertNotEqual((snapshot / relative).stat().st_ino, (self.state / relative).stat().st_ino)

    def test_checkpoint_preserves_sqlite_wal_and_shared_memory_bytes(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("INSERT INTO user_data VALUES ('committed in WAL')")
            connection.commit()
            self.assertTrue(self.database.with_name("sqlite.db-wal").is_file())
            original = contents(self.state)
            with self.store.lock():
                record = self.begin()
                snapshot = self.store.checkpoint(record)
                self.assertEqual(contents(snapshot), original)
                connection.close()
                connection = None
                self.migrate()
                self.store.restore(record)
                self.assertEqual(contents(self.state), original)
                with closing(sqlite3.connect(self.database)) as restored:
                    self.assertEqual(
                        restored.execute("SELECT value FROM user_data ORDER BY rowid").fetchall(),
                        [("original user data",), ("committed in WAL",)],
                    )
                    self.assertEqual(restored.execute("SELECT version FROM tree_go_migrations").fetchall(), [(1,)])
        finally:
            if connection is not None:
                connection.close()

    def test_manual_backups_and_unrelated_root_entries_are_not_copied_or_changed(self):
        manual = self.state / ".predeploy-operator-checkpoint"
        (manual / "jobs").mkdir(parents=True)
        (manual / "jobs/operator.sqlite").write_bytes(b"operator-managed backup")
        (self.state / ".predeploy-empty").mkdir()
        (self.state / "unrelated").write_bytes(b"operator note")
        with self.store.lock():
            record = self.begin()
            snapshot = self.store.checkpoint(record)
            self.assertEqual(set(path.name for path in snapshot.iterdir()), set(DATASET))
            self.migrate()
            (manual / "jobs/operator.sqlite").write_bytes(b"operator update during release")
            (self.state / "candidate-root-note").write_bytes(b"outside managed dataset")
            rescue = self.store.restore(record)
            self.assertEqual(set(path.name for path in rescue.iterdir()), {"jobs", "objects", "session"})
        self.assertEqual((manual / "jobs/operator.sqlite").read_bytes(), b"operator update during release")
        self.assertTrue((self.state / ".predeploy-empty").is_dir())
        self.assertEqual((self.state / "unrelated").read_bytes(), b"operator note")
        self.assertEqual((self.state / "candidate-root-note").read_bytes(), b"outside managed dataset")

    def test_failed_checkpoint_never_becomes_ready_and_can_be_retried(self):
        original = contents(self.state)
        with self.store.lock():
            record = self.begin()
            with patch.object(recovery, "_copy_file", side_effect=OSError(errno.ENOSPC, "checkpoint full")):
                with self.assertRaises(OSError):
                    self.store.checkpoint(record)
            self.assertEqual(self.store.pending()["phase"], "begun")
            self.assertFalse(Path(record["checkpoint"]).exists())
            self.assertEqual(contents(self.state), original)
            self.assertIsNone(self.store.restore(record))
            self.assertTrue((self.store.directory / "blocked").exists())
            with self.assertRaises(recovery.RecoveryError):
                self.store.complete(record, "activated")
            self.store.checkpoint(record)
            self.assertEqual(self.store.pending()["phase"], "ready")
            self.assertEqual(contents(Path(record["checkpoint"])), original)

    def test_interrupted_begin_is_blocked_and_recovers_without_a_checkpoint(self):
        original = contents(self.state)
        with self.store.lock():
            with patch.object(self.store, "_publish", side_effect=OSError(errno.ENOSPC, "journal full")):
                with self.assertRaises(OSError):
                    self.begin()
            self.assertFalse((self.store.directory / "pending.json").exists())
            record = self.store.pending()
            self.assertEqual(record["phase"], "begun")
            self.assertTrue((self.store.directory / "blocked").exists())
            self.assertIsNone(self.store.restore(record))
            self.assertEqual(contents(self.state), original)
            self.store.admit(record)
            self.store.complete(record, "restored")
            self.assertIsNone(self.store.pending())

    def test_interrupted_checkpoint_publication_is_not_readiness(self):
        original = contents(self.state)
        publish = self.store._publish

        def fail_ready(record):
            if record["phase"] == "ready":
                raise OSError(errno.ENOSPC, "journal full")
            publish(record)

        with self.store.lock():
            record = self.begin()
            with patch.object(self.store, "_publish", side_effect=fail_ready):
                with self.assertRaises(OSError):
                    self.store.checkpoint(record)
            self.assertEqual(self.store.pending()["phase"], "begun")
            self.assertTrue(Path(record["checkpoint"]).is_dir())
            self.assertIsNone(self.store.restore(record))
            self.assertEqual(contents(self.state), original)
            self.store.checkpoint(record)
            self.assertEqual(self.store.pending()["phase"], "ready")

    def test_restore_failure_stays_blocked_and_replays_without_overwriting_first_rescue(self):
        original = contents(self.state)
        with self.store.lock():
            record = self.begin()
            snapshot = self.store.checkpoint(record)
            self.migrate()
            failed = contents(self.state)
            copy_component = recovery._copy_component

            def fail_install(source, destination):
                if source == snapshot / "jobs":
                    raise OSError(errno.ENOSPC, "restore full")
                copy_component(source, destination)

            with patch.object(recovery, "_copy_component", side_effect=fail_install):
                with self.assertRaises(OSError):
                    self.store.restore(record)
            self.assertEqual(self.store.pending()["phase"], "restoring")
            self.assertFalse((self.state / "jobs").exists())
            rescue = Path(record["rescue"])
            self.assertEqual(contents(rescue), failed)
            self.assertTrue((self.store.directory / "blocked").exists())
            with self.assertRaises(recovery.RecoveryError):
                self.store.admit(record)
            (self.state / "session").write_text("interrupted partial restoration")
        replacement = recovery.RecoveryStore(self.store.directory)
        with replacement.lock():
            record = replacement.pending()
            self.assertEqual(replacement.restore(record), rescue)
            self.assertEqual(contents(self.state), original)
            self.assertEqual(contents(rescue), failed)
            replacement.admit(record)
            replacement.complete(record, "restored")

    def test_rescue_copy_failure_does_not_mutate_live_dataset(self):
        with self.store.lock():
            record = self.begin()
            self.store.checkpoint(record)
            self.migrate()
            failed = contents(self.state)
            with patch.object(recovery, "_copy_file", side_effect=OSError(errno.ENOSPC, "rescue full")):
                with self.assertRaises(OSError):
                    self.store.restore(record)
            self.assertEqual(contents(self.state), failed)
            self.assertEqual(self.store.pending()["phase"], "ready")
            self.assertIsNone(record["rescue"])
            self.assertTrue((self.store.directory / "blocked").exists())
            rescue = self.store.restore(record)
            self.assertEqual(contents(rescue), failed)

    def test_interruption_after_rescue_publication_keeps_first_failed_dataset(self):
        original = contents(self.state)
        with self.store.lock():
            record = self.begin()
            self.store.checkpoint(record)
            self.migrate()
            failed = contents(self.state)
            publish = self.store._publish

            def fail_restoring(value):
                if value["phase"] == "restoring":
                    raise OSError(errno.ENOSPC, "journal full after rescue")
                publish(value)

            with patch.object(self.store, "_publish", side_effect=fail_restoring):
                with self.assertRaises(OSError):
                    self.store.restore(record)
            self.assertEqual(self.store.pending()["phase"], "ready")
            self.assertEqual(contents(self.state), failed)
            rescue = Path(record["checkpoint"]).parent / "rescue"
            self.assertEqual(contents(rescue), failed)
            (self.state / "session").write_text("later interrupted-recovery state")
            self.assertEqual(self.store.restore(record), rescue)
            self.assertEqual(contents(self.state), original)
            self.assertEqual(contents(rescue), failed)

    def test_restoration_is_idempotent_after_old_runtime_has_written_again(self):
        with self.store.lock():
            record = self.begin()
            self.store.checkpoint(record)
            self.migrate()
            rescue = self.store.restore(record)
            rescued = contents(rescue)
            self.store.admit(record)
            with closing(sqlite3.connect(self.database)) as database, database:
                database.execute("INSERT INTO user_data VALUES ('new old-runtime write')")
            (self.state / "session").write_text("old runtime changed session")
            resumed = contents(self.state)
            self.assertEqual(self.store.restore(record), rescue)
            self.assertEqual(contents(self.state), resumed)
            self.assertEqual(contents(rescue), rescued)
            self.assertTrue((self.store.directory / "blocked").exists())

    def test_admitted_candidate_recovery_retains_post_admission_writes(self):
        original = contents(self.state)
        with self.store.lock():
            record = self.begin()
            self.store.checkpoint(record)
            self.migrate()
            self.store.admit(record)
            (self.state / "jobs/post-admission").write_bytes(b"accepted candidate write")
        replacement = recovery.RecoveryStore(self.store.directory)
        with replacement.lock():
            record = replacement.pending()
            self.assertTrue(record["admitted"])
            rescue = replacement.restore(record)
            self.assertEqual(contents(self.state), original)
            self.assertEqual((rescue / "jobs/post-admission").read_bytes(), b"accepted candidate write")
            self.assertEqual(record["phase"], "restored")

    def test_completion_retains_history_and_does_not_prune_other_checkpoints(self):
        with self.store.lock():
            first = self.begin()
            snapshot = self.store.checkpoint(first)
            self.store.admit(first)
            self.store.complete(first, "activated")
            self.assertIsNone(self.store.pending())
            self.assertFalse((self.store.directory / "blocked").exists())
            second = self.begin()
            self.assertNotEqual(second["id"], first["id"])
            self.assertTrue(snapshot.is_dir())
            history = json.loads((snapshot.parent / "record.json").read_text())
            self.assertEqual(history["outcome"], "activated")
            self.store.restore(second)
            self.store.admit(second)
            self.store.complete(second, "restored")
            self.assertTrue(snapshot.is_dir())

    def test_interrupted_completion_retains_durable_history_and_can_complete_again(self):
        with self.store.lock():
            record = self.begin()
            snapshot = self.store.checkpoint(record)
            self.store.admit(record)
            unlink = Path.unlink
            pending = self.store.directory / "pending.json"

            def fail_pending_unlink(path, *args, **kwargs):
                if path == pending:
                    raise OSError(errno.EIO, "interrupted finalization")
                return unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_pending_unlink):
                with self.assertRaises(OSError):
                    self.store.complete(record, "activated")
            self.assertEqual(self.store.pending()["outcome"], "activated")
            self.assertFalse((self.store.directory / "blocked").exists())
            self.assertEqual(json.loads((snapshot.parent / "record.json").read_text())["outcome"], "activated")
            self.store.complete(record, "activated")
            self.assertIsNone(self.store.pending())
            self.assertTrue(snapshot.is_dir())

    def test_private_records_and_data_have_private_control_ancestors(self):
        with self.store.lock():
            record = self.begin()
            snapshot = self.store.checkpoint(record)
            self.migrate()
            rescue = self.store.restore(record)
            self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o755)
            for name in ("lock", "pending.json", "blocked"):
                path = self.store.directory / name
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.stat().st_uid, os.geteuid())
            for path in (snapshot.parent.parent, snapshot.parent, snapshot, rescue):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                self.assertEqual(path.stat().st_uid, os.geteuid())
            self.store.admit(record)
            self.store.complete(record, "restored")
            self.assertEqual(stat.S_IMODE((snapshot.parent / "record.json").stat().st_mode), 0o600)

    def test_concurrent_operation_is_denied_without_replacing_lock(self):
        with self.store.lock():
            inode = (self.store.directory / "lock").stat().st_ino
            contender = recovery.RecoveryStore(self.store.directory)
            with self.assertRaisesRegex(recovery.RecoveryError, "another Tree lifecycle"):
                with contender.lock():
                    self.fail("concurrent lock acquired")
            self.assertEqual((self.store.directory / "lock").stat().st_ino, inode)
        with contender.lock():
            self.assertIsNone(contender.pending())

    def test_recovery_root_remains_traversable_with_private_process_umask(self):
        previous = os.umask(0o077)
        try:
            with self.store.lock():
                record = self.begin()
                self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE((self.store.directory / "blocked").stat().st_mode), 0o600)
                snapshot = self.store.checkpoint(record)
                self.assertEqual(stat.S_IMODE(snapshot.parent.stat().st_mode), 0o700)
        finally:
            os.umask(previous)

    def test_mutations_require_same_instance_operation_lock(self):
        with self.assertRaisesRegex(recovery.RecoveryError, "operation lock"):
            self.begin()
        with self.store.lock():
            record = self.begin()
            contender = recovery.RecoveryStore(self.store.directory)
            with self.assertRaisesRegex(recovery.RecoveryError, "operation lock"):
                contender.checkpoint(record)
            with self.assertRaisesRegex(recovery.RecoveryError, "unresolved"):
                self.begin()

    def test_unsafe_recovery_roots_and_ancestor_symlinks_fail_closed(self):
        actual = self.root / "actual"
        actual.mkdir()
        link = self.root / "linked"
        link.symlink_to(actual, target_is_directory=True)
        for path in (link, link / "recovery"):
            with self.subTest(path=path), self.assertRaises(recovery.RecoveryError):
                with recovery.RecoveryStore(path).lock():
                    self.fail("unsafe path accepted")
        writable = self.root / "writable"
        writable.mkdir(mode=0o777)
        writable.chmod(0o777)
        with self.assertRaises(recovery.RecoveryError):
            with recovery.RecoveryStore(writable).lock():
                self.fail("world-writable root accepted")
        for path in (Path("relative"), self.root / ".." / "elsewhere", Path("/")):
            with self.subTest(path=path), self.assertRaises(recovery.RecoveryError):
                recovery.RecoveryStore(path)

    def test_private_lock_or_pending_symlinks_fail_closed(self):
        secret = self.root / "outside"
        secret.write_text("do not read or overwrite")
        self.store.directory.mkdir(mode=0o755)
        self.store.directory.chmod(0o755)
        (self.store.directory / "lock").symlink_to(secret)
        with self.assertRaises(recovery.RecoveryError):
            with self.store.lock():
                self.fail("symlink lock accepted")
        (self.store.directory / "lock").unlink()
        with self.store.lock():
            (self.store.directory / "pending.json").symlink_to(secret)
            with self.assertRaises(recovery.RecoveryError):
                self.store.pending()
        self.assertEqual(secret.read_text(), "do not read or overwrite")

    def test_malformed_pending_records_fail_closed(self):
        with self.store.lock():
            record = self.begin()
            pending = self.store.directory / "pending.json"
            for value in (
                {},
                dict(record, phase=[]),
                dict(record, checkpoint="/tmp/untrusted"),
                dict(record, rescue="/tmp/untrusted"),
                dict(record, state="/"),
                dict(record, id="../../untrusted"),
                dict(record, outcome=[]),
            ):
                with self.subTest(value=value):
                    pending.write_text(json.dumps(value))
                    with self.assertRaises(recovery.RecoveryError):
                        self.store.pending()
            pending.write_text("{not JSON")
            with self.assertRaises(recovery.RecoveryError):
                self.store.pending()
            self.assertTrue((self.store.directory / "blocked").exists())

    def test_public_pending_permissions_are_rejected(self):
        with self.store.lock():
            self.begin()
            pending = self.store.directory / "pending.json"
            pending.chmod(0o644)
            with self.assertRaisesRegex(recovery.RecoveryError, "unsafe private recovery record"):
                self.store.pending()
            self.assertTrue((self.store.directory / "blocked").exists())

    def test_required_and_nested_state_symlinks_fail_closed(self):
        secret = self.root / "outside"
        secret.write_text("untouched")
        with self.store.lock():
            (self.state / "session").unlink()
            (self.state / "session").symlink_to(secret)
            with self.assertRaises(recovery.RecoveryError):
                self.begin()
            self.assertIsNone(self.store.pending())
            (self.state / "session").unlink()
            (self.state / "session").write_text("original-session\n")
            record = self.begin()
            (self.state / "objects/escape").symlink_to(secret)
            with self.assertRaises(recovery.RecoveryError):
                self.store.checkpoint(record)
            self.assertEqual(self.store.pending()["phase"], "begun")
            self.assertTrue((self.store.directory / "blocked").exists())
            self.assertIsNone(self.store.restore(record))
        self.assertEqual(secret.read_text(), "untouched")

    def test_extended_attributes_and_exact_modes_survive_restore(self):
        path = self.state / "jobs/application.json"
        try:
            os.setxattr(path, "user.tree-recovery", b"original metadata")
        except OSError as error:
            if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                self.skipTest("filesystem does not support user xattrs")
            raise
        path.chmod(0o640)
        original = contents(self.state)
        with self.store.lock():
            record = self.begin()
            self.store.checkpoint(record)
            os.setxattr(path, "user.tree-recovery", b"candidate metadata")
            path.chmod(0o600)
            self.store.restore(record)
            self.assertEqual(contents(self.state), original)
            self.assertEqual(os.getxattr(path, "user.tree-recovery"), b"original metadata")

    @unittest.skipUnless(os.geteuid() == 0, "distinct service ownership requires root")
    def test_original_service_uid_gid_are_restored_not_root_ownership(self):
        for path in self.state.rglob("*"):
            os.chown(path, 65534, 65534)
        original = contents(self.state)
        with self.store.lock():
            record = self.begin()
            snapshot = self.store.checkpoint(record)
            self.assertEqual((snapshot / "jobs/sqlite.db").stat().st_uid, 65534)
            self.migrate()
            self.store.restore(record)
            self.assertEqual(contents(self.state), original)
            self.assertEqual(self.database.stat().st_uid, 65534)
            self.assertEqual(self.database.stat().st_gid, 65534)

    @unittest.skipUnless(os.geteuid() == 0, "unsafe service ownership fixture requires root")
    def test_service_owned_recovery_root_is_rejected(self):
        self.store.directory.mkdir(mode=0o755)
        self.store.directory.chmod(0o755)
        os.chown(self.store.directory, 65534, 65534)
        with self.assertRaisesRegex(recovery.RecoveryError, "unsafe recovery root"):
            with self.store.lock():
                self.fail("service-owned recovery root accepted")


if __name__ == "__main__":
    unittest.main()
