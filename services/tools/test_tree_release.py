import argparse
from contextlib import closing
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(os.environ.get("HOMELAB_TEST_TOOLS", str(Path(__file__).parent)))
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location("tree_release", TOOLS / "tree-dev.py")
tree = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tree)

OLD = "sha256:" + "1" * 64
CANDIDATE = "sha256:" + "2" * 64
REVISION = "a" * 40
STABLE = "localhost/tree-eclass:stable"


def dataset(path):
    """Compare real managed bytes, ownership, modes and file timestamps."""
    result = {}
    for name in ("jobs", "objects", "session", "code"):
        component = path / name
        entries = [component, *sorted(component.rglob("*"))] if component.is_dir() else [component]
        for entry in entries:
            info = entry.lstat()
            metadata = (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
            if entry.is_dir():
                value = ("directory", metadata)
            elif entry.is_symlink():
                value = ("symlink", metadata, os.readlink(entry))
            else:
                value = ("file", metadata, info.st_mtime_ns, entry.read_bytes())
            result[str(entry.relative_to(path))] = value
    return result


class Runtime:
    """Only process, OCI transfer, and service boundaries are simulated."""

    def __init__(self, case):
        self.case = case
        self.events = []
        self.images = {
            "tree-dev": {CANDIDATE: CANDIDATE, f"localhost/tree-eclass:{REVISION}": CANDIDATE},
            "tree-eclass": {OLD: OLD, STABLE: OLD},
        }
        self.units = {unit: "inactive" for unit in tree.PRODUCTION_UNITS}
        self.units["tree-eclass.service"] = "active"
        self.launcher_alive = False
        self.delayed_launch_request = False
        self.blocked_launch_requests = 0
        self.migration_result = {"Result": "success", "ExecMainCode": "1", "ExecMainStatus": "0"}
        self.reported_migration_status = None
        self.lost_migration_unit = False
        self.containers = {}
        self.add_container("tree-eclass")
        self.preflight_failure = False
        self.build_failure = False
        self.transfer_failure = False
        self.health_failure = False
        self.old_health_failure = False
        self.migration_failure = None
        self.unsafe_candidate = False
        self.bad_stop_probe = False
        self.stop_failure = False
        self.extra_writer_after_stop = False
        self.preflight_state = "active"
        self.old_expected = case.original
        self.clock = 0

    def tick(self):
        self.clock += 100
        return self.clock

    def result(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def add_container(self, name, *, live=True, source=None):
        self.containers[name] = {
            "Name": name,
            "Id": hashlib.sha256(name.encode()).hexdigest(),
            "State": {"Running": live, "Restarting": False},
            "Mounts": [{"Source": str(source or self.case.prod / "jobs"), "RW": True}],
        }

    def process(self, arguments, **kwargs):
        if arguments[0] != "git":
            raise AssertionError(f"unexpected process: {arguments}")
        if "status" in arguments:
            self.events.append("git-status")
            return self.result()
        if "rev-parse" in arguments:
            return self.result(stdout=REVISION + "\n")
        raise AssertionError(f"unexpected git operation: {arguments}")

    def archive(self, arguments, **kwargs):
        self.case.assertIn("archive", arguments)
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode="w") as archive:
            for name in ("Dockerfile", "backend/go.mod"):
                archive.add(self.case.checkout / name, arcname=name)
        content.seek(0)
        process = Mock(stdout=content)
        process.wait.return_value = 0
        process.poll.return_value = 0
        return process

    def transfer(self, source, target, image):
        self.case.assertEqual((source, target), ("tree-dev", "tree-eclass"))
        self.case.assertEqual(self.units["tree-eclass.service"], self.preflight_state)
        self.events.append("transfer")
        if self.transfer_failure:
            raise tree.PrincipalError("OCI transfer failed")
        identity = self.images[source][image]
        self.images[target][identity] = identity
        return identity

    def run_as(self, role, arguments, **kwargs):
        if arguments[0] == "systemctl":
            self.case.assertEqual(role, "tree-eclass" if "tree-eclass-dev.service" not in arguments else "tree-dev")
            return self.systemctl(*arguments[2:], timeout=kwargs.get("timeout", 120))
        if arguments[0] == "podman":
            return self.podman(*arguments[1:], role=role, **kwargs)
        if arguments[0] == "image_update":
            self.case.assertEqual(role, "image-updates")
            self.case.assertIn("--require-aged", arguments)
            self.case.assertEqual(self.units["tree-eclass.service"], self.preflight_state)
            self.events.append("authorize-bases")
            if self.preflight_failure:
                return self.result(1, stdout=b"", stderr=b"bases are not seven-day eligible")
            return self.result(
                stdout=json.dumps(
                    {
                        "specification": self.case.specification,
                        "rendered": self.case.rendered,
                    }
                ).encode(),
                stderr=b"",
            )
        raise AssertionError(f"unexpected principal process: {role} {arguments}")

    def fire_pending_launches(self):
        if self.delayed_launch_request:
            if not (self.case.recovery / "migration.env").exists():
                self.blocked_launch_requests += 1
                self.units[tree.DEPLOY_MIGRATION_UNIT] = "failed"
            else:
                self.launcher_alive = True
                self.units[tree.DEPLOY_MIGRATION_UNIT] = "activating"
            self.delayed_launch_request = False
        if self.launcher_alive:
            self.add_container("tree-eclass-deploy-migrate")
            (self.case.prod / "jobs/late-launch-write").write_bytes(b"writer launched after cold proof")

    def start_migration(self, timeout):
        permit = self.case.recovery / "migration.env"
        if not permit.is_file():
            return self.result(1, stderr="mandatory migration.env is missing")
        record = self.case.store.pending()
        self.case.assertEqual(record["phase"], "ready")
        image = permit.read_text().strip().partition("=")[2].strip("'\"")
        self.case.assertEqual(image, record["candidate"])
        self.case.assertTrue(Path(record["checkpoint"]).is_dir())
        self.case.assertTrue((self.case.recovery / "blocked").exists())
        retained = f"localhost/tree-eclass:checkpoint-{record['id']}"
        self.case.assertEqual(self.images["tree-eclass"][retained], OLD)
        self.case.assertTrue(
            all(self.units[unit] == "inactive" for unit in tree.PRODUCTION_UNITS if unit != tree.DEPLOY_MIGRATION_UNIT)
        )
        self.case.assertFalse(any(tree.writing_container(item, self.case.prod) for item in self.containers.values()))
        self.units[tree.DEPLOY_MIGRATION_UNIT] = "activating"
        self.launcher_alive = True
        if self.migration_failure in ("launcher-timeout", "launcher-interrupt"):
            # The caller disappears before the unit's podman child materializes
            # a container, and another StartUnit request remains delayed.
            self.delayed_launch_request = True
            if self.migration_failure == "launcher-timeout":
                raise subprocess.TimeoutExpired(["systemctl", "start", tree.DEPLOY_MIGRATION_UNIT], timeout)
            raise KeyboardInterrupt()
        self.add_container("tree-eclass-deploy-migrate")
        self.case.mutate()
        if self.unsafe_candidate:
            (self.case.prod / "jobs" / "unsafe-link").symlink_to(self.case.root / "outside")
        self.events.append("migration")
        if self.migration_failure == "timeout":
            raise subprocess.TimeoutExpired(["systemctl", "start", tree.DEPLOY_MIGRATION_UNIT], timeout)
        if self.migration_failure == "interrupt":
            raise KeyboardInterrupt()
        del self.containers["tree-eclass-deploy-migrate"]
        self.launcher_alive = False
        exit_status = 1 if self.migration_failure else 0
        self.migration_result["ExecMainStatus"] = str(
            exit_status if self.reported_migration_status is None else self.reported_migration_status
        )
        self.migration_result["Result"] = "success" if exit_status == 0 else "exit-code"
        self.units[tree.DEPLOY_MIGRATION_UNIT] = "active" if exit_status == 0 else "failed"
        return self.result(exit_status, stderr="candidate migration failed" if exit_status else "")

    def systemctl(self, *arguments, timeout=120):
        operation, unit = arguments[0], arguments[-1]
        if unit == "tree-eclass-dev.service":
            return self.result(stdout="inactive\n")
        self.case.assertIn(unit, tree.PRODUCTION_UNITS)
        if operation == "reset-failed":
            self.units[unit] = "inactive"
            return self.result()
        if operation == "show":
            if "--property=LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus" in arguments:
                properties = dict(
                    self.migration_result,
                    LoadState="not-found" if self.lost_migration_unit else "loaded",
                    ActiveState=self.units[unit],
                    SubState="exited" if self.units[unit] == "active" else "failed",
                )
                return self.result(stdout="\n".join(f"{key}={value}" for key, value in properties.items()))
            if self.bad_stop_probe and self.units[unit] == "inactive":
                return self.result(1, stderr="user manager unavailable")
            return self.result(stdout=self.units[unit] + "\n")
        if operation == "stop":
            self.events.append("stop:" + unit)
            if self.stop_failure:
                return self.result(1, stderr="user manager stop failed")
            self.units[unit] = "inactive"
            if unit == tree.DEPLOY_MIGRATION_UNIT:
                self.case.assertFalse(
                    (self.case.recovery / "migration.env").exists(), "migration permit must be revoked before cold stop"
                )
                self.launcher_alive = False
            if self.extra_writer_after_stop:
                self.add_container("operator-owned-writer")
            name = unit.removesuffix(".service")
            if name in self.containers:
                self.containers[name]["State"]["Running"] = False
            return self.result()
        if operation == "start":
            if unit == tree.DEPLOY_MIGRATION_UNIT:
                return self.start_migration(timeout)
            self.case.assertEqual(unit, "tree-eclass.service")
            self.case.assertFalse((self.case.recovery / "blocked").exists())
            self.fire_pending_launches()
            image = self.images["tree-eclass"][STABLE]
            if image == OLD:
                self.case.assertEqual(
                    dataset(self.case.prod), self.old_expected, "old runtime started before exact data restoration"
                )
            self.events.append("start:" + image)
            self.units[unit] = "active"
            self.add_container("tree-eclass")
            return self.result()
        raise AssertionError(f"unexpected service operation: {arguments}")

    def podman(self, *arguments, role, **kwargs):
        images = self.images[role]
        self.events.append((role, arguments))
        if arguments[:2] == ("image", "exists"):
            reference = arguments[-1]
            if "@sha256:" in reference:
                return self.result()
            return self.result(0 if reference in images else 1)
        if arguments[:2] == ("image", "inspect"):
            image = images.get(arguments[-1])
            if image is None:
                return self.result(1, stderr="image missing")
            if arguments[3] == "{{json .Labels}}":
                labels = self.case.labels if image == CANDIDATE else {tree.REVISION_LABEL: "b" * 40}
                return self.result(stdout=json.dumps(labels))
            return self.result(stdout=image + "\n")
        if arguments[0] == "build":
            self.case.assertEqual(role, "tree-dev")
            self.case.assertEqual(self.units["tree-eclass.service"], self.preflight_state)
            self.case.assertIsNone(self.case.store.pending())
            self.case.assertIn("--pull=never", arguments)
            self.events.append("build-while-running")
            if self.build_failure:
                return self.result(1, stderr="build failed")
            images[arguments[arguments.index("-t") + 1]] = CANDIDATE
            images[CANDIDATE] = CANDIDATE
            return self.result()
        self.case.assertEqual(role, "tree-eclass")
        if arguments[0] == "ps":
            return self.result(stdout="\n".join(item["Id"] for item in self.containers.values()))
        if arguments[:2] == ("container", "inspect"):
            selected = [item for item in self.containers.values() if item["Id"] in arguments[2:]]
            return self.result(stdout=json.dumps(selected))
        if arguments[0] == "stop":
            for name, item in self.containers.items():
                if name in arguments or item["Id"] in arguments:
                    item["State"]["Running"] = False
            return self.result()
        if arguments[0] == "rm":
            for name in arguments[1:]:
                if name not in self.containers:
                    continue
                self.case.assertFalse(self.containers[name]["State"]["Running"])
                del self.containers[name]
            return self.result()
        if arguments[0] == "tag":
            if arguments[1] not in images:
                return self.result(1, stderr="image missing")
            images[arguments[2]] = images[arguments[1]]
            return self.result()
        if arguments[0] == "exec":
            image = images[STABLE]
            failed = self.health_failure if image == CANDIDATE else self.old_health_failure
            return self.result(1 if failed else 0)
        raise AssertionError(f"unexpected OCI operation: {arguments}")


class ReleaseLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prod = self.root / "production"
        self.recovery = self.root / "recovery"
        self.checkout = self.root / "checkout"
        (self.checkout / ".git").mkdir(parents=True)
        (self.checkout / "backend").mkdir()
        (self.checkout / "backend/go.mod").write_text("module disposable-test\n")
        (self.checkout / "Dockerfile").write_text("FROM docker.io/example/base:1\n")
        (self.prod / "jobs/private").mkdir(parents=True, mode=0o700)
        (self.prod / "objects/nested").mkdir(parents=True, mode=0o700)
        (self.prod / "session").write_bytes(b"prior-session\n")
        (self.prod / "code").write_bytes(b"prior-code\n")
        (self.prod / "jobs/application.json").write_bytes(b'{"private": "prior-setting"}\n')
        (self.prod / "jobs/private/settings.json").write_bytes(b'{"secret": "private-original"}\n')
        (self.prod / "objects/nested/original").write_bytes(b"prior-object\x00\xff")
        # Copy a real cold SQLite/WAL dataset while its source connection still
        # retains WAL; close only the separate source, never the disposable prod.
        seed = self.root / "seed.db"
        with closing(sqlite3.connect(seed)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE tree_go_migrations (version integer)")
            connection.execute("INSERT INTO tree_go_migrations VALUES (1)")
            connection.commit()
            for suffix in ("", "-wal", "-shm"):
                shutil.copyfile(Path(str(seed) + suffix), self.prod / "jobs" / ("sqlite.db" + suffix))
        for index, entry in enumerate(sorted(self.prod.rglob("*"))):
            entry.chmod(0o700 if entry.is_dir() else 0o600)
            if entry.is_file():
                os.utime(entry, ns=(1_700_000_000_000_000_000 + index, 1_700_000_000_000_000_000 + index))
        self.original = dataset(self.prod)
        manual = self.prod / ".predeploy-manual"
        manual.mkdir()
        (manual / "keep").write_bytes(b"operator-owned checkpoint")
        self.store = tree.RecoveryStore(self.recovery)
        self.specification = {
            "schema": 1,
            "dockerfile_digest": tree.dockerfile_digest(self.checkout / "Dockerfile"),
            "images": {"docker.io/example/base:1": {"reference": "docker.io/example/base@sha256:" + "3" * 64}},
        }
        self.rendered = "FROM docker.io/example/base@sha256:" + "3" * 64 + "\n"
        self.labels = {
            tree.REVISION_LABEL: REVISION,
            tree.BASE_IMAGES_LABEL: tree.base_provenance(self.specification),
            tree.DOCKERFILE_DIGEST_LABEL: self.specification["dockerfile_digest"],
        }
        self.runtime = Runtime(self)
        patches = [
            patch.dict(
                os.environ, {"HOMELAB_TREE_CHECKOUT": str(self.checkout), "HOMELAB_TREE_RECOVERY": str(self.recovery)}
            ),
            patch.object(
                tree,
                "principal",
                return_value={
                    "uid": os.geteuid(),
                    "gid": os.getegid(),
                    "home": str(self.root),
                    "stateRoot": str(self.root),
                },
            ),
            patch.object(
                tree, "principal_for_unit", side_effect=lambda unit: "tree-dev" if "-dev" in unit else "tree-eclass"
            ),
            patch.object(tree, "tool", side_effect=lambda name: name),
            patch.object(tree, "prod_state", return_value=self.prod),
            patch.object(tree, "run_as", side_effect=self.runtime.run_as),
            patch.object(tree, "stream_image", side_effect=self.runtime.transfer),
            patch.object(tree.subprocess, "run", side_effect=self.runtime.process),
            patch.object(tree.subprocess, "Popen", side_effect=self.runtime.archive),
            patch.object(tree.time, "time", side_effect=self.runtime.tick),
            patch.object(tree.time, "monotonic", side_effect=self.runtime.tick),
            patch.object(tree.time, "sleep"),
        ]
        for patched in patches:
            patched.start()
            self.addCleanup(patched.stop)

    def mutate(self):
        with closing(sqlite3.connect(self.prod / "jobs/sqlite.db")) as connection, connection:
            connection.execute("ALTER TABLE tree_go_migrations ADD COLUMN candidate text")
            connection.execute("UPDATE tree_go_migrations SET version=2, candidate='migrated'")
            connection.execute("CREATE TABLE candidate_only (value text)")
            connection.execute("INSERT INTO candidate_only VALUES ('new-data')")
        (self.prod / "jobs/application.json").write_bytes(b'{"private": "candidate-setting"}\n')
        (self.prod / "jobs/private/settings.json").unlink()
        (self.prod / "jobs/candidate-only").write_bytes(b"new mutable state")
        replacement = self.prod / "objects/atomic-replacement"
        replacement.write_bytes(b"atomically published candidate object")
        replacement.chmod(0o600)
        replacement.replace(self.prod / "objects/nested/original")
        (self.prod / "objects/candidate-only").write_bytes(b"new-object")
        (self.prod / "session").write_bytes(b"candidate-session\n")
        (self.prod / "code").write_bytes(b"candidate-code\n")

    def deploy(self):
        with self.store.lock():
            return tree.cmd_deploy(argparse.Namespace(recovery=self.store))

    def recover(self):
        # Reopen the actual durable journal, as a new CLI invocation would.
        self.store = tree.RecoveryStore(self.recovery)
        with self.store.lock():
            return tree.cmd_recover(argparse.Namespace(recovery=self.store))

    def assert_restored(self):
        self.assertEqual(dataset(self.prod), self.original)
        self.assertIsNone(self.store.pending())
        self.assertFalse((self.recovery / "blocked").exists())
        self.assertFalse((self.recovery / "migration.env").exists())
        self.assertEqual(self.runtime.images["tree-eclass"][STABLE], OLD)
        self.assertEqual((self.prod / ".predeploy-manual/keep").read_bytes(), b"operator-owned checkpoint")
        self.assertIn("start:" + OLD, self.runtime.events)

    def test_migration_failure_restores_real_database_wal_settings_objects_and_identity(self):
        self.runtime.migration_failure = "failure"
        with self.assertRaisesRegex(tree.DevError, "production migration failed"):
            self.deploy()
        self.assert_restored()
        rescue = next((self.recovery / "checkpoints").glob("*/rescue"))
        self.assertEqual((rescue / "objects/candidate-only").read_bytes(), b"new-object")
        self.assertNotEqual(dataset(rescue), self.original)
        self.assertEqual((rescue / "jobs/application.json").stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect((rescue / "jobs/sqlite.db").as_uri() + "?mode=ro", uri=True)) as database:
            self.assertEqual(database.execute("SELECT version FROM tree_go_migrations").fetchall(), [(2,)])
            self.assertEqual(database.execute("SELECT value FROM candidate_only").fetchall(), [("new-data",)])
        with closing(sqlite3.connect((self.prod / "jobs/sqlite.db").as_uri() + "?mode=ro", uri=True)) as database:
            self.assertEqual(database.execute("SELECT version FROM tree_go_migrations").fetchall(), [(1,)])
            self.assertEqual(
                database.execute("SELECT name FROM sqlite_master WHERE name='candidate_only'").fetchall(), []
            )

    def test_health_failure_restores_data_before_old_runtime_starts(self):
        self.runtime.health_failure = True
        with self.assertRaisesRegex(tree.DevError, "did not answer /api/health"):
            self.deploy()
        self.assert_restored()
        self.assertIn("start:" + CANDIDATE, self.runtime.events)

    def test_unproven_migration_exit_restores_data_instead_of_admitting_candidate(self):
        self.runtime.reported_migration_status = 137
        with self.assertRaisesRegex(tree.DevError, "migration completion could not be proven"):
            self.deploy()
        self.assert_restored()
        self.assertNotIn("start:" + CANDIDATE, self.runtime.events)

    def test_lost_migration_unit_cannot_admit_migrated_candidate(self):
        self.runtime.lost_migration_unit = True
        with self.assertRaisesRegex(tree.DevError, "migration unit was lost or unavailable"):
            self.deploy()
        self.assert_restored()
        self.assertNotIn("start:" + CANDIDATE, self.runtime.events)

    def test_timeout_stops_surviving_migration_before_restoring(self):
        self.runtime.migration_failure = "timeout"
        with self.assertRaises(subprocess.TimeoutExpired):
            self.deploy()
        self.assert_restored()
        self.assertNotIn("tree-eclass-deploy-migrate", self.runtime.containers)

    def test_keyboard_interruption_restores_prior_data_and_rethrows_interruption(self):
        self.runtime.migration_failure = "interrupt"
        with self.assertRaises(KeyboardInterrupt):
            self.deploy()
        self.assert_restored()

    def test_unmaterialized_launcher_and_delayed_request_cannot_write_after_recovery(self):
        for failure in ("launcher-timeout", "launcher-interrupt"):
            with self.subTest(failure=failure):
                self.runtime.migration_failure = failure
                expected = subprocess.TimeoutExpired if failure == "launcher-timeout" else KeyboardInterrupt
                with self.assertRaises(expected):
                    self.deploy()
                self.runtime.fire_pending_launches()
                self.assert_restored()
                self.assertFalse(self.runtime.launcher_alive)
                self.assertFalse((self.recovery / "migration.env").exists())
                self.assertNotIn("tree-eclass-deploy-migrate", self.runtime.containers)
        self.assertEqual(self.runtime.blocked_launch_requests, 2)

    def test_preflight_failures_leave_existing_runtime_and_data_running(self):
        for failure in ("preflight_failure", "transfer_failure"):
            with self.subTest(failure=failure):
                setattr(self.runtime, failure, True)
                with self.assertRaises((tree.DevError, tree.PrincipalError)):
                    self.deploy()
                setattr(self.runtime, failure, False)
                self.assertEqual(dataset(self.prod), self.original)
                self.assertEqual(self.runtime.units["tree-eclass.service"], "active")
                self.assertIsNone(self.store.pending())
                self.assertFalse((self.recovery / "blocked").exists())
                self.assertFalse(
                    any(isinstance(event, str) and event.startswith("stop:") for event in self.runtime.events)
                )

    def test_build_precedes_stop_and_failed_build_never_stops_runtime(self):
        del self.runtime.images["tree-dev"][f"localhost/tree-eclass:{REVISION}"]
        self.runtime.build_failure = True
        with self.assertRaisesRegex(tree.DevError, "podman build"):
            self.deploy()
        self.assertEqual(self.runtime.units["tree-eclass.service"], "active")
        self.assertIsNone(self.store.pending())
        self.assertEqual(dataset(self.prod), self.original)
        self.runtime.build_failure = False
        self.assertEqual(self.deploy(), 0)
        self.assertLess(
            self.runtime.events.index("build-while-running"), self.runtime.events.index("stop:tree-eclass.service")
        )
        self.assertEqual(self.runtime.images["tree-eclass"][STABLE], CANDIDATE)
        self.assertIsNone(self.store.pending())
        checkpoint = next((self.recovery / "checkpoints").glob("*/snapshot"))
        self.assertEqual(dataset(checkpoint), self.original)
        retained = [value for name, value in self.runtime.images["tree-eclass"].items() if ":checkpoint-" in name]
        self.assertEqual(retained, [OLD])

    def test_stop_includes_active_normal_and_lingering_deploy_migrations(self):
        self.runtime.units["tree-eclass-migrate.service"] = "active"
        self.runtime.add_container("tree-eclass-migrate")
        self.runtime.add_container("tree-eclass-deploy-migrate")
        self.assertEqual(self.deploy(), 0)
        self.assertNotIn("tree-eclass-migrate", self.runtime.containers)
        self.assertNotIn("tree-eclass-deploy-migrate", self.runtime.containers)

    def test_unknown_container_writer_is_not_stopped_and_preflight_fails_closed(self):
        self.runtime.add_container("operator-owned-writer")
        with self.assertRaisesRegex(tree.DevError, "operator-owned-writer"):
            self.deploy()
        self.assertTrue(self.runtime.containers["operator-owned-writer"]["State"]["Running"])
        self.assertEqual(self.runtime.units["tree-eclass.service"], "active")
        self.assertIsNone(self.store.pending())
        self.assertEqual(dataset(self.prod), self.original)

    def test_unknown_unit_state_never_authorizes_checkpoint_or_migration(self):
        self.runtime.bad_stop_probe = True
        with self.assertRaisesRegex(tree.DevError, "tree-dev recover"):
            self.deploy()
        self.assertEqual(self.store.pending()["phase"], "begun")
        self.assertTrue((self.recovery / "blocked").exists())
        self.assertNotIn("migration", self.runtime.events)
        self.assertNotIn("start:" + OLD, self.runtime.events)
        self.assertFalse(any(item["State"]["Running"] for item in self.runtime.containers.values()))
        self.runtime.bad_stop_probe = False
        self.assertEqual(self.recover(), 0)
        self.assert_restored()

    def test_failed_user_manager_stop_still_stops_known_containers_and_blocks(self):
        self.runtime.stop_failure = True
        self.runtime.add_container("tree-eclass-deploy-migrate")
        with self.assertRaisesRegex(tree.DevError, "user manager stop failed.*tree-dev recover"):
            self.deploy()
        self.assertEqual(self.store.pending()["phase"], "begun")
        self.assertTrue((self.recovery / "blocked").exists())
        self.assertFalse(any(item["State"]["Running"] for item in self.runtime.containers.values()))
        self.assertNotIn("migration", self.runtime.events)
        self.assertEqual(dataset(self.prod), self.original)
        self.runtime.stop_failure = False
        self.assertEqual(self.recover(), 0)
        self.assert_restored()

    def test_new_external_writer_after_preflight_blocks_without_stopping_it(self):
        self.runtime.extra_writer_after_stop = True
        with self.assertRaisesRegex(tree.DevError, "operator-owned-writer.*tree-dev recover"):
            self.deploy()
        self.assertEqual(self.store.pending()["phase"], "begun")
        self.assertTrue((self.recovery / "blocked").exists())
        self.assertTrue(self.runtime.containers["operator-owned-writer"]["State"]["Running"])
        self.assertNotIn("migration", self.runtime.events)
        self.assertEqual(dataset(self.prod), self.original)
        self.runtime.extra_writer_after_stop = False
        self.runtime.containers["operator-owned-writer"]["State"]["Running"] = False
        self.assertEqual(self.recover(), 0)
        self.assert_restored()

    def test_failed_real_restore_remains_blocked_and_recover_is_actionable(self):
        self.runtime.migration_failure = "failure"
        self.runtime.unsafe_candidate = True
        with self.assertRaisesRegex(tree.DevError, "recovery failed.*tree-dev recover"):
            self.deploy()
        self.assertTrue((self.recovery / "blocked").exists())
        self.assertIsNotNone(self.store.pending())
        self.assertNotIn("start:" + OLD, self.runtime.events)
        self.assertFalse(any(item["State"]["Running"] for item in self.runtime.containers.values()))
        self.assertNotEqual(dataset(self.prod), self.original)
        (self.prod / "jobs/unsafe-link").unlink()
        self.assertEqual(self.recover(), 0)
        self.assert_restored()

    def interrupted_record(self, *, admitted=False):
        with self.store.lock():
            record = self.store.begin(self.prod, OLD, CANDIDATE, True)
            tree.stop_production_writers(self.prod)
            self.store.checkpoint(record)
            tree.retain_previous_image(record)
            self.mutate()
            self.runtime.images["tree-eclass"][CANDIDATE] = CANDIDATE
            tree.set_stable(CANDIDATE)
            tree.remove_production_container()
            if admitted:
                self.store.admit(record)
                tree.start_prod()
            return record

    def test_explicit_recover_reopens_interrupted_journal_and_restores_exact_snapshot(self):
        self.interrupted_record()
        self.assertEqual(self.recover(), 0)
        self.assert_restored()

    def test_admitted_interrupted_candidate_warns_and_retains_post_checkpoint_writes(self):
        record = self.interrupted_record(admitted=True)
        (self.prod / "jobs/post-admission").write_bytes(b"writes after admission")
        interrupted = dataset(self.prod)
        output = io.StringIO()
        with patch.object(tree.sys, "stderr", output):
            self.assertEqual(self.recover(), 0)
        self.assert_restored()
        self.assertIn("discards writes", output.getvalue())
        self.assertEqual(dataset(Path(record["checkpoint"]).parent / "rescue"), interrupted)

    def test_missing_previous_image_blocks_without_image_only_or_stable_fallback(self):
        record = self.interrupted_record()
        images = self.runtime.images["tree-eclass"]
        del images[OLD]
        del images[f"localhost/tree-eclass:checkpoint-{record['id']}"]
        failed = dataset(self.prod)
        with self.assertRaisesRegex(tree.DevError, "tree-dev recover"):
            self.recover()
        self.assertEqual(dataset(self.prod), failed)
        self.assertTrue((self.recovery / "blocked").exists())
        self.assertNotIn("start:" + OLD, self.runtime.events)
        self.assertEqual(images[STABLE], CANDIDATE)
        images[OLD] = OLD
        self.assertEqual(self.recover(), 0)
        self.assert_restored()

    def test_recover_before_checkpoint_leaves_original_data_untouched(self):
        with self.store.lock():
            self.store.begin(self.prod, OLD, CANDIDATE, True)
        self.assertEqual(self.recover(), 0)
        self.assert_restored()

    def test_interrupted_completed_restore_does_not_rewind_old_runtime_writes(self):
        record = self.interrupted_record()
        with self.store.lock():
            self.store.restore(record)
            tree.set_stable(tree.retain_previous_image(record))
            self.store.admit(record)
            tree.start_prod()
        (self.prod / "jobs/old-runtime-write").write_bytes(b"committed after restoration")
        restored_with_writes = dataset(self.prod)
        self.runtime.old_expected = restored_with_writes
        self.assertEqual(self.recover(), 0)
        self.assertEqual(dataset(self.prod), restored_with_writes)
        self.assertIsNone(self.store.pending())
        self.assertFalse((self.recovery / "blocked").exists())

    def test_failed_old_runtime_health_stays_stopped_and_blocked_until_recovery(self):
        self.runtime.health_failure = True
        self.runtime.old_health_failure = True
        with self.assertRaisesRegex(tree.DevError, "recovery failed.*tree-dev recover"):
            self.deploy()
        self.assertEqual(dataset(self.prod), self.original)
        self.assertEqual(self.store.pending()["phase"], "restored")
        self.assertTrue((self.recovery / "blocked").exists())
        self.assertEqual(self.runtime.units["tree-eclass.service"], "inactive")
        self.runtime.old_health_failure = False
        self.assertEqual(self.recover(), 0)
        self.assert_restored()

    def test_previously_stopped_runtime_is_not_restarted_after_failed_activation(self):
        self.runtime.units["tree-eclass.service"] = "inactive"
        self.runtime.containers["tree-eclass"]["State"]["Running"] = False
        self.runtime.preflight_state = "inactive"
        self.runtime.migration_failure = "failure"
        with self.assertRaisesRegex(tree.DevError, "production migration failed"):
            self.deploy()
        self.assertEqual(dataset(self.prod), self.original)
        self.assertIsNone(self.store.pending())
        self.assertFalse(any(isinstance(event, str) and event.startswith("start:") for event in self.runtime.events))

    def test_pending_state_for_another_path_is_rejected_before_any_cold_mutation(self):
        other = self.root / "other-state"
        shutil.copytree(self.prod, other)
        with self.store.lock():
            self.store.begin(other, OLD, CANDIDATE, True)
        before = list(self.runtime.events)
        with self.assertRaisesRegex(tree.DevError, "declared production state"):
            self.recover()
        self.assertEqual(self.runtime.events, before)
        self.assertEqual(dataset(self.prod), self.original)
        self.assertTrue((self.recovery / "blocked").exists())

    def test_main_serializes_mutations_and_refuses_pending_for_all_other_commands(self):
        with self.store.lock():
            self.store.begin(self.prod, OLD, CANDIDATE, True)
        for command in ("up", "sync", "down", "stage", "deploy"):
            with (
                self.subTest(command=command),
                patch.object(tree, "require_admin"),
                patch.object(tree.os, "getuid", return_value=0),
                patch.object(tree.os, "chdir"),
                patch.object(tree.os, "umask"),
                patch.object(tree.sys, "stderr", io.StringIO()) as output,
            ):
                self.assertEqual(tree.main([command]), 1)
                self.assertIn("tree-dev recover", output.getvalue())
        self.assertEqual(dataset(self.prod), self.original)
        with (
            self.store.lock(),
            patch.object(tree, "require_admin"),
            patch.object(tree.os, "getuid", return_value=0),
            patch.object(tree.os, "chdir"),
            patch.object(tree.os, "umask"),
            patch.object(tree.sys, "stderr", io.StringIO()) as output,
        ):
            self.assertEqual(tree.main(["recover"]), 1)
            self.assertIn("holds the recovery lock", output.getvalue())

    def test_cli_reports_original_failure_after_successful_data_recovery(self):
        self.runtime.migration_failure = "failure"
        with (
            patch.object(tree, "require_admin"),
            patch.object(tree.os, "getuid", return_value=0),
            patch.object(tree.os, "chdir"),
            patch.object(tree.os, "umask"),
            patch.object(tree.sys, "stderr", io.StringIO()) as output,
        ):
            self.assertEqual(tree.main(["deploy"]), 1)
        self.assertIn("previous code and data restored", output.getvalue())
        self.assertIn("production migration failed", output.getvalue())
        self.assertNotIn("tree-dev recover", output.getvalue())
        self.assert_restored()


if __name__ == "__main__":
    unittest.main()
