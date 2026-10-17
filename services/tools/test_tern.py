from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from credential_client import CredentialError
from credential_validation import OPTIONAL, parse_payload, payload_bytes, required_fields, validate_fields, web_token_from_snippet, web_token_snippet


def load_module(name, filename):
    path = Path(__file__).with_name(filename)
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "programs" / "tern" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


secrets = load_module("homelab_secrets", "homelab-secrets.py")
tern = load_module("tern_under_test", "tool.py")
import homelab_client as client_module

OLD = "refresh-token-old-0123456789"
NEW = "refresh-token-new-0123456789"
TOKEN = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
LATEST_SHA = "ab" * 32


class FakeBroker:
    """In-memory broker with the real store's compare-and-swap digest contract."""

    def __init__(self):
        self.records = {}
        self.failures = []
        self.write_then_fail = 0

    @staticmethod
    def digest(value):
        return None if value is None else hashlib.sha256(value).hexdigest()

    def read(self, component):
        value = self.records.get(component)
        return value, self.digest(value)

    def store(self, component, payload, expected):
        if self.failures:
            raise self.failures.pop(0)
        if expected != self.digest(self.records.get(component)):
            raise CredentialError("conflict")
        self.records[component] = payload
        if self.write_then_fail:
            self.write_then_fail -= 1
            raise CredentialError("rollback")

    def put(self, component, fields):
        self.records[component] = payload_bytes(component, fields)

    def fields(self, component):
        return parse_payload(component, self.records[component], complete=False)


class BrokerCase(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker()
        runtime = tempfile.TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        self.runtime = Path(runtime.name)
        self.runtime.chmod(0o700)
        for patcher in (
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)}),
            patch.object(secrets, "read_record", side_effect=self.broker.read),
            # prepare now runs through the thin client; its reads take the
            # broker's complete flag, which the fake accepts and ignores.
            patch.object(client_module, "read_record", side_effect=lambda component, complete=False: self.broker.read(component)),
            patch.object(secrets, "store_record", side_effect=self.broker.store),
            patch.object(tern, "sleep"),
            # Certificate checks need real PEM and openssl; token logic is under test.
            patch("credential_validation._tls"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class SessionTests(BrokerCase):
    def setUp(self):
        super().setUp()
        self.broker.put("tern", {"stencil-refresh-token": OLD})

    def answer(self, status, body):
        return patch.object(tern, "http", return_value=(status, json.dumps(body).encode()))

    def test_successor_token_is_saved_before_the_access_token_is_used(self):
        seen = []

        def build_service(access):
            seen.append(self.broker.fields("tern")["stencil-refresh-token"])
            raise tern.ToolError("stop here")

        with tempfile.TemporaryDirectory() as root:
            checkout = Path(root)
            (checkout / ".git").mkdir()
            (checkout / "flake.nix").write_text("{}")
            with self.answer(200, {"access_token": "access", "refresh_token": NEW}), \
                    patch.object(tern, "latest_build", side_effect=build_service):
                with self.assertRaises(tern.ToolError):
                    tern.update(checkout, check_only=False)
        self.assertEqual(seen, [NEW])

    def test_rejected_session_asks_for_login_and_keeps_the_record(self):
        with self.answer(400, {"error": "invalid_grant", "error_description": "refresh token was already used"}):
            with self.assertRaises(tern.ToolError) as error:
                tern.refresh()
        self.assertIn("homelab-tern login", str(error.exception))
        self.assertEqual(self.broker.fields("tern")["stencil-refresh-token"], OLD)

    def test_signed_out_state_asks_for_login_without_any_request(self):
        self.broker.records.clear()
        with patch.object(tern, "http") as http, self.assertRaises(tern.ToolError) as error:
            tern.refresh()
        self.assertIn("homelab-tern login", str(error.exception))
        http.assert_not_called()

    def test_transient_broker_failures_are_retried_until_the_token_is_saved(self):
        self.broker.failures = [CredentialError("unavailable"), CredentialError("unavailable")]
        tern.save_token(NEW)
        self.assertEqual(self.broker.fields("tern")["stencil-refresh-token"], NEW)

    def test_a_save_that_landed_before_the_error_counts_as_saved(self):
        self.broker.write_then_fail = 1
        tern.save_token(NEW)
        self.assertEqual(self.broker.fields("tern")["stencil-refresh-token"], NEW)

    def test_a_concurrent_edit_is_retried_against_the_fresh_record(self):
        original = self.broker.read
        calls = {"n": 0}

        def read_once_stale(component):
            calls["n"] += 1
            value, digest = original(component)
            # Someone else changes the record between our first read and our store.
            if calls["n"] == 1:
                self.broker.records["tern"] = payload_bytes("tern", {"stencil-refresh-token": "someone-else-0123456789"})
            return value, digest

        with patch.object(secrets, "read_record", side_effect=read_once_stale):
            tern.save_token(NEW)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.broker.fields("tern")["stencil-refresh-token"], NEW)

    def test_unsaved_successor_aborts_and_says_the_old_token_is_revoked(self):
        self.broker.failures = [CredentialError("unavailable")] * tern.SAVE_ATTEMPTS
        with self.assertRaises(tern.ToolError) as error:
            tern.save_token(NEW)
        self.assertIn("already revoked", str(error.exception))
        self.assertIn("homelab-tern login", str(error.exception))
        self.assertNotIn(NEW, str(error.exception))
        self.assertEqual(self.broker.fields("tern")["stencil-refresh-token"], OLD)

    def test_login_saves_the_session_and_never_prints_the_device_code(self):
        device = {"device_code": "device-secret-code", "user_code": "ABCD-EFGH", "verification_uri": "https://auth.example/device",
                  "verification_uri_complete": "https://auth.example/device?code=ABCD-EFGH", "expires_in": 900, "interval": 5}
        replies = [
            (200, json.dumps(device).encode()),
            (400, json.dumps({"error": "authorization_pending"}).encode()),
            (400, json.dumps({"error": "slow_down"}).encode()),
            (200, json.dumps({"refresh_token": NEW, "access_token": "a"}).encode()),
        ]
        self.broker.records.clear()
        shown = io.StringIO()
        with patch.object(tern, "http", side_effect=replies), redirect_stderr(shown), redirect_stdout(shown):
            tern.login()
        self.assertEqual(self.broker.fields("tern")["stencil-refresh-token"], NEW)
        self.assertIn("ABCD-EFGH", shown.getvalue())
        self.assertNotIn("device-secret-code", shown.getvalue())
        self.assertNotIn(NEW, shown.getvalue())

    def test_declined_or_expired_login_saves_nothing(self):
        device = {"device_code": "d", "user_code": "U", "verification_uri": "https://auth.example/device", "expires_in": 900}
        for error in ("access_denied", "expired_token"):
            self.broker.records.clear()
            replies = [(200, json.dumps(device).encode()), (400, json.dumps({"error": error}).encode())]
            with patch.object(tern, "http", side_effect=replies), redirect_stderr(io.StringIO()), self.assertRaises(tern.ToolError):
                tern.login()
            self.assertNotIn("tern", self.broker.records)


class HttpTests(unittest.TestCase):
    def run_http(self, **arguments):
        captured = {}

        def fake(argv, input, **options):
            captured.update(argv=argv, config=input.decode())
            return subprocess.CompletedProcess(argv, 0, stdout=b'{"ok":true}200', stderr=b"")

        with patch.object(tern.subprocess, "run", side_effect=fake):
            result = tern.http("https://auth.stencil.so/oauth/token", **arguments)
        return result, captured

    def test_secrets_travel_on_stdin_only_and_are_escaped(self):
        (status, body), captured = self.run_http(bearer="bearer-secret", form={"refresh_token": 'a"b\\c'})
        self.assertEqual((status, body), (200, b'{"ok":true}'))
        self.assertEqual(captured["argv"][1:], ["--config", "-"])
        self.assertNotIn("bearer-secret", " ".join(captured["argv"]))
        self.assertIn('data-urlencode = "refresh_token=a\\"b\\\\c"', captured["config"])
        self.assertIn('proto = "=https"', captured["config"])

    def test_values_that_could_inject_config_lines_are_refused(self):
        with self.assertRaises(tern.ToolError):
            tern.http("https://auth.stencil.so/", form={"x": 'v"\nurl = "http://evil'})

    def test_unreachable_host_is_a_clean_error(self):
        with patch.object(tern.subprocess, "run", return_value=subprocess.CompletedProcess([], 6, stdout=b"", stderr=b"")):
            with self.assertRaises(tern.ToolError) as error:
                tern.http("https://build.stencil.so/x")
        self.assertIn("build.stencil.so", str(error.exception))


def build_answer(**overrides):
    build = {
        "id": "20261012-142610-e2b8656", "version": "0.8.3", "channel": "stable",
        "files": [
            {"name": "Tern-0.8.3-linux-arm64.tar.gz", "size": 1, "sha256": "cd" * 32, "url": "/d/tern/x/Tern-0.8.3-linux-arm64.tar.gz"},
            {"name": "Tern-0.8.3-linux-x86_64.tar.gz", "size": 71066346, "sha256": LATEST_SHA,
             "url": "https://evil.example/elsewhere.tar.gz"},
        ],
    }
    build.update(overrides)
    return (200, json.dumps({"build": build}).encode())


class BuildTests(unittest.TestCase):
    def test_download_url_is_built_from_the_build_id_not_taken_from_the_api(self):
        with patch.object(tern, "http", return_value=build_answer()):
            build = tern.latest_build("access")
        self.assertEqual(build["url"], "https://build.stencil.so/d/tern/20261012-142610-e2b8656/Tern-0.8.3-linux-x86_64.tar.gz")
        self.assertEqual(build["sha256"], LATEST_SHA)

    def test_unsafe_or_ambiguous_builds_are_rejected(self):
        files = build_answer()[1]
        cases = {
            "unstable": build_answer(channel="beta"),
            "odd version": build_answer(version="0.8.3-rc1"),
            "odd id": build_answer(id="../../etc"),
            "no file": build_answer(files=[]),
        }
        bad_sha = json.loads(files)
        bad_sha["build"]["files"][1]["sha256"] = "XYZ"
        cases["bad sha"] = (200, json.dumps(bad_sha).encode())
        for label, reply in cases.items():
            with self.subTest(label), patch.object(tern, "http", return_value=reply), self.assertRaises(tern.ToolError):
                tern.latest_build("access")

    def test_refused_session_mentions_account_access(self):
        with patch.object(tern, "http", return_value=(403, b'{"error":"forbidden"}')), self.assertRaises(tern.ToolError) as error:
            tern.latest_build("access")
        self.assertIn("lack Tern access", str(error.exception))

    def test_download_that_does_not_match_the_published_hash_is_discarded(self):
        build = {"name": "Tern-0.8.3-linux-x86_64.tar.gz", "url": "https://build.stencil.so/d/x", "size": 5, "sha256": LATEST_SHA}

        def write(url, **options):
            Path(options["output"]).write_bytes(b"12345")
            return 200, b""

        with tempfile.TemporaryDirectory() as scratch, patch.object(tern, "http", side_effect=write):
            with self.assertRaises(tern.ToolError) as error:
                tern.download(build, "access", scratch)
            self.assertIn("discarded", str(error.exception))
            self.assertEqual(os.listdir(scratch), [])

    def test_matching_download_is_returned(self):
        payload = b"tarball bytes"
        build = {"name": "Tern-0.8.3-linux-x86_64.tar.gz", "url": "https://build.stencil.so/d/x", "size": len(payload),
                 "sha256": hashlib.sha256(payload).hexdigest()}

        def write(url, **options):
            Path(options["output"]).write_bytes(payload)
            return 200, b""

        with tempfile.TemporaryDirectory() as scratch, patch.object(tern, "http", side_effect=write):
            self.assertEqual(tern.download(build, "access", scratch).read_bytes(), payload)

    def test_matching_hash_with_wrong_published_size_is_discarded(self):
        payload = b"tarball bytes"
        build = {"name": "Tern-0.8.3-linux-x86_64.tar.gz", "url": "https://build.stencil.so/d/x",
                 "size": len(payload) + 1, "sha256": hashlib.sha256(payload).hexdigest()}

        def write(url, **options):
            Path(options["output"]).write_bytes(payload)
            return 200, b""

        with tempfile.TemporaryDirectory() as scratch, patch.object(tern, "http", side_effect=write):
            with self.assertRaises(tern.ToolError):
                tern.download(build, "access", scratch)
            self.assertEqual(os.listdir(scratch), [])


def write_archive(path, members=None):
    if members is None:
        members = [("tern", tarfile.DIRTYPE, b"", 0o755),
                   ("tern/tern", tarfile.REGTYPE, b"executable", 0o755)]
    with tarfile.open(path, "w:gz") as archive:
        for name, kind, payload, mode in members:
            member = tarfile.TarInfo(name)
            member.type, member.mode = kind, mode
            member.size = len(payload)
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = "/outside"
            archive.addfile(member, io.BytesIO(payload))


class ArchiveTests(unittest.TestCase):
    def test_only_the_release_layout_and_regular_executable_are_accepted(self):
        root = ("tern", tarfile.DIRTYPE, b"", 0o755)
        binary = ("tern/tern", tarfile.REGTYPE, b"executable", 0o755)
        cases = {
            "unexpected member": [root, binary, ("other", tarfile.REGTYPE, b"x", 0o644)],
            "traversal": [root, binary, ("tern/../outside", tarfile.REGTYPE, b"x", 0o644)],
            "duplicate binary": [root, binary, binary],
            "duplicate root": [root, root, binary],
            "aliased duplicate": [root, binary, ("./tern/tern", tarfile.REGTYPE, b"x", 0o755)],
            "missing binary": [root],
            "regular root": [("tern", tarfile.REGTYPE, b"x", 0o755), binary],
            "symlink root": [("tern", tarfile.SYMTYPE, b"", 0o755), binary],
            "symlink binary": [root, ("tern/tern", tarfile.SYMTYPE, b"", 0o755)],
            "hardlink binary": [root, ("tern/tern", tarfile.LNKTYPE, b"", 0o755)],
            "directory binary": [root, ("tern/tern", tarfile.DIRTYPE, b"", 0o755)],
            "fifo binary": [root, ("tern/tern", tarfile.FIFOTYPE, b"", 0o755)],
            "empty binary": [root, ("tern/tern", tarfile.REGTYPE, b"", 0o755)],
            "nonexecutable binary": [root, ("tern/tern", tarfile.REGTYPE, b"x", 0o644)],
        }
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "release.tar.gz"
            for label, members in cases.items():
                with self.subTest(label):
                    write_archive(path, members)
                    with self.assertRaises(tern.ToolError):
                        tern.verify_archive(path)
            for members in ([root, binary], [binary], [
                    ("./tern/", tarfile.DIRTYPE, b"", 0o755),
                    ("./tern/tern", tarfile.REGTYPE, b"x", 0o755)]):
                write_archive(path, members)
                tern.verify_archive(path)

    def test_nonarchive_and_truncated_archive_are_clean_errors(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "release.tar.gz"
            path.write_bytes(b"<html>sign in</html>")
            with self.assertRaises(tern.ToolError):
                tern.verify_archive(path)
            write_archive(path)
            path.write_bytes(path.read_bytes()[:20])
            with self.assertRaises(tern.ToolError):
                tern.verify_archive(path)


class UpdateTests(BrokerCase):
    def setUp(self):
        super().setUp()
        self.broker.put("tern", {"stencil-refresh-token": OLD})
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.checkout = Path(root.name)
        (self.checkout / ".git").mkdir()
        (self.checkout / "flake.nix").write_text("{}")
        (self.checkout / "programs" / "tern").mkdir(parents=True)
        self.pin = self.checkout / "programs" / "tern" / "source.json"
        self.original = json.dumps({"version": "0.8.2", "hash": "sha256-old"}).encode() + b"\n"
        self.pin.write_bytes(self.original)
        self.build = {"version": "0.8.3", "name": "Tern-0.8.3-linux-x86_64.tar.gz", "sha256": LATEST_SHA, "size": 3,
                      "url": "https://build.stencil.so/d/tern/20261012-142610-e2b8656/Tern-0.8.3-linux-x86_64.tar.gz"}
        self.archive = self.checkout / self.build["name"]
        write_archive(self.archive)
        self.commands = []
        self.real_download = tern.download
        for patcher in (
            patch.object(tern, "refresh", return_value="access"),
            patch.object(tern, "latest_build", side_effect=lambda access: dict(self.build)),
            patch.object(tern, "download", return_value=self.archive),
            patch.object(tern, "command_output", side_effect=self.command),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def command(self, argv, check=True):
        self.commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="/nix/store/verified\n")

    def run_update(self, **options):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            tern.update(self.checkout, options.pop("check_only", False))
        return output.getvalue()

    def pin_to(self, version, digest):
        self.pin.write_text(json.dumps({"version": version, "hash": digest}) + "\n")

    def test_never_downgrades(self):
        self.pin_to("0.9.0", "sha256-x")
        with patch.object(tern, "download") as download:
            self.assertIn("nothing to do", self.run_update())
        download.assert_not_called()

    def test_refuses_a_changed_file_for_the_pinned_version(self):
        self.pin_to("0.8.3", "sha256-" + "AAAA" * 10 + "AAA=")
        with patch.object(tern, "download") as download, self.assertRaises(tern.ToolError) as error:
            self.run_update()
        self.assertIn("different file", str(error.exception))
        download.assert_not_called()

    def test_check_only_downloads_and_changes_nothing(self):
        with patch.object(tern, "download") as download:
            self.assertIn("latest: 0.8.3", self.run_update(check_only=True))
        download.assert_not_called()
        self.assertEqual(self.pin.read_bytes(), self.original)

    def test_current_pin_already_in_the_store_is_left_alone(self):
        self.pin_to("0.8.3", tern.sri(LATEST_SHA))
        with patch.object(tern, "in_store", return_value=True), patch.object(tern, "download") as download:
            self.assertIn("already in the Nix store", self.run_update())
        download.assert_not_called()

    def test_failed_verification_build_restores_the_previous_pin(self):
        self.pin.chmod(0o640)

        def fail_build(argv, check=True):
            if "build" in argv:
                raise tern.ToolError("nix exited with status 1")
            return self.command(argv, check)

        with patch.object(tern, "command_output", side_effect=fail_build):
            with self.assertRaises(tern.ToolError):
                self.run_update()
        self.assertEqual(self.pin.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.pin.stat().st_mode), 0o640)

    def test_independent_edit_during_the_build_is_not_undone(self):
        def fail_after_edit(argv, check=True):
            if "build" in argv:
                self.pin.write_text("someone else edited this\n")
                raise tern.ToolError("nix exited with status 1")
            return self.command(argv, check)

        with patch.object(tern, "command_output", side_effect=fail_after_edit):
            with self.assertRaises(tern.ToolError):
                self.run_update()
        self.assertEqual(self.pin.read_text(), "someone else edited this\n")

    def test_fetched_release_is_added_pinned_and_built_without_a_helper(self):
        self.pin.chmod(0o640)
        self.assertIn("pinned and verified", self.run_update())
        self.assertEqual(json.loads(self.pin.read_bytes()), {"version": "0.8.3", "hash": tern.sri(LATEST_SHA)})
        self.assertEqual(stat.S_IMODE(self.pin.stat().st_mode), 0o640)
        self.assertEqual(self.commands[0][1:], ["--add-fixed", "sha256", str(self.archive)])
        self.assertEqual(self.commands[1][1:], [
            "build", "--no-update-lock-file", "--no-link", "--print-out-paths", f"{self.checkout}#tern"])

    def test_same_pin_with_missing_source_still_builds_and_keeps_pin_bytes(self):
        self.pin_to("0.8.3", tern.sri(LATEST_SHA))
        original = tern.pin_state(self.pin)
        with patch.object(tern, "in_store", return_value=False):
            self.assertIn("pinned and verified", self.run_update())
        self.assertEqual(tern.pin_state(self.pin), original)
        self.assertEqual(len(self.commands), 2)
        self.assertIn("build", self.commands[-1])

    def test_same_pin_missing_source_reports_failed_build(self):
        self.pin_to("0.8.3", tern.sri(LATEST_SHA))
        original = tern.pin_state(self.pin)

        def fail_build(argv, check=True):
            if "build" in argv:
                raise tern.ToolError("nix exited with status 1")
            return self.command(argv, check)

        with patch.object(tern, "in_store", return_value=False), \
                patch.object(tern, "command_output", side_effect=fail_build), self.assertRaises(tern.ToolError):
            self.run_update()
        self.assertEqual(tern.pin_state(self.pin), original)

    def test_invalid_archive_never_changes_pin_or_store(self):
        write_archive(self.archive, [("tern/tern", tarfile.SYMTYPE, b"", 0o755)])
        with self.assertRaises(tern.ToolError):
            self.run_update()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.pin.read_bytes(), self.original)

    def test_edit_while_fetching_is_rejected_before_store_or_pin_mutation(self):
        def edit(build, access, directory):
            self.pin.write_text("independent edit\n")
            return self.archive

        with patch.object(tern, "download", side_effect=edit), self.assertRaises(tern.ToolError) as error:
            self.run_update()
        self.assertIn("changed independently", str(error.exception))
        self.assertEqual(self.commands, [])
        self.assertEqual(self.pin.read_text(), "independent edit\n")

    def test_edit_while_importing_is_rejected_before_pin_mutation(self):
        def edit(argv, check=True):
            self.pin.write_text("independent edit\n")
            return self.command(argv, check)

        with patch.object(tern, "command_output", side_effect=edit), self.assertRaises(tern.ToolError):
            self.run_update()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.pin.read_text(), "independent edit\n")

    def test_edit_at_atomic_publication_is_not_overwritten(self):
        real_fsync = os.fsync

        def edit(descriptor):
            real_fsync(descriptor)
            self.pin.write_text("independent edit\n")

        with patch.object(tern.os, "fsync", side_effect=edit), self.assertRaises(tern.ToolError):
            self.run_update()
        self.assertEqual(self.pin.read_text(), "independent edit\n")
        self.assertEqual(len(self.commands), 1)

    def test_successful_build_with_independent_edit_is_reported_as_failure(self):
        def edit(argv, check=True):
            if "build" in argv:
                self.pin.write_text("independent edit\n")
            return self.command(argv, check)

        with patch.object(tern, "command_output", side_effect=edit), self.assertRaises(tern.ToolError) as error:
            self.run_update()
        self.assertIn("changed independently", str(error.exception))
        self.assertEqual(self.pin.read_text(), "independent edit\n")

    def test_successful_build_with_removed_pin_is_reported_as_failure(self):
        def remove(argv, check=True):
            if "build" in argv:
                self.pin.unlink()
            return self.command(argv, check)

        with patch.object(tern, "command_output", side_effect=remove), self.assertRaises(tern.ToolError):
            self.run_update()
        self.assertFalse(self.pin.exists())

    def test_store_failure_never_changes_pin(self):
        with patch.object(tern, "command_output", side_effect=tern.ToolError("nix-store exited with status 1")), \
                self.assertRaises(tern.ToolError):
            self.run_update()
        self.assertEqual(self.pin.read_bytes(), self.original)

    def test_invalid_build_output_restores_pin(self):
        for output in ("", "relative/path\n", "/nix/store/one\n/nix/store/two\n"):
            with self.subTest(output=output), patch.object(
                    tern, "command_output", return_value=subprocess.CompletedProcess([], 0, stdout=output)), \
                    self.assertRaises(tern.ToolError):
                self.run_update()
            self.assertEqual(self.pin.read_bytes(), self.original)

    def test_initial_pin_is_removed_if_build_fails(self):
        self.pin.unlink()

        def fail_build(argv, check=True):
            if "build" in argv:
                raise tern.ToolError("nix exited with status 1")
            return self.command(argv, check)

        with patch.object(tern, "command_output", side_effect=fail_build), self.assertRaises(tern.ToolError):
            self.run_update()
        self.assertFalse(self.pin.exists())

    def test_failed_build_restores_atomically(self):
        real_replace = os.replace
        publications = []

        def replace(source, target):
            self.assertEqual(Path(target), self.pin)
            publications.append(Path(source).read_bytes())
            real_replace(source, target)

        def fail_build(argv, check=True):
            if "build" in argv:
                raise tern.ToolError("nix exited with status 1")
            return self.command(argv, check)

        with patch.object(tern.os, "replace", side_effect=replace), \
                patch.object(tern, "command_output", side_effect=fail_build), self.assertRaises(tern.ToolError):
            self.run_update()
        self.assertEqual(len(publications), 2)
        self.assertEqual(json.loads(publications[0]), {"version": "0.8.3", "hash": tern.sri(LATEST_SHA)})
        self.assertEqual(publications[1], self.original)

    def test_independent_edit_while_staging_rollback_is_not_overwritten(self):
        real_fsync = os.fsync
        writes = 0

        def edit(descriptor):
            nonlocal writes
            real_fsync(descriptor)
            writes += 1
            if writes == 2:
                self.pin.write_text("independent edit\n")

        def fail_build(argv, check=True):
            if "build" in argv:
                raise tern.ToolError("nix exited with status 1")
            return self.command(argv, check)

        with patch.object(tern.os, "fsync", side_effect=edit), \
                patch.object(tern, "command_output", side_effect=fail_build), self.assertRaises(tern.ToolError) as error:
            self.run_update()
        self.assertIn("nix exited with status 1", str(error.exception))
        self.assertEqual(self.pin.read_text(), "independent edit\n")

    def test_independent_mode_edit_during_build_is_preserved_and_reported(self):
        self.pin.chmod(0o644)

        def edit(argv, check=True):
            if "build" in argv:
                self.pin.chmod(0o600)
            return self.command(argv, check)

        with patch.object(tern, "command_output", side_effect=edit), self.assertRaises(tern.ToolError):
            self.run_update()
        self.assertEqual(stat.S_IMODE(self.pin.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.pin.read_bytes())["version"], "0.8.3")

    def test_independent_same_content_replacement_is_not_rolled_back(self):
        def edit(argv, check=True):
            if "build" in argv:
                other = self.pin.with_name("independent.json")
                other.write_bytes(self.pin.read_bytes())
                other.replace(self.pin)
            return self.command(argv, check)

        with patch.object(tern, "command_output", side_effect=edit), self.assertRaises(tern.ToolError):
            self.run_update()
        self.assertEqual(json.loads(self.pin.read_bytes())["version"], "0.8.3")

    def test_completed_download_verifies_published_bytes_before_import(self):
        payload = self.archive.read_bytes()
        self.build.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())

        def write(url, **options):
            self.assertEqual(options["bearer"], "access")
            Path(options["output"]).write_bytes(payload)
            return 200, b""

        with patch.object(tern, "download", side_effect=self.real_download), patch.object(tern, "http", side_effect=write):
            self.assertIn("pinned and verified", self.run_update())
        self.assertEqual(json.loads(self.pin.read_bytes())["hash"], tern.sri(self.build["sha256"]))
        self.assertEqual(len(self.commands), 2)


class WebTokenTests(BrokerCase):
    def test_write_delivers_the_bare_token_privately_with_a_trailing_newline(self):
        self.broker.put("nginx", {"omp-web-token.conf": web_token_snippet(TOKEN)})
        target = self.runtime / "tern" / "web-token"
        tern.web_token_write(target, wait=0)
        self.assertEqual(target.read_text(), TOKEN + "\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_write_without_a_stored_token_says_how_to_create_one(self):
        self.broker.put("nginx", {"tls.crt": "x"})
        with self.assertRaises(tern.ToolError) as error:
            tern.web_token_write(self.runtime / "tern" / "web-token", wait=0)
        self.assertIn("web-token generate", str(error.exception))
        self.assertFalse((self.runtime / "tern" / "web-token").exists())

    def test_write_waits_for_a_broker_that_is_not_up_yet(self):
        self.broker.put("nginx", {"omp-web-token.conf": web_token_snippet(TOKEN)})
        original = self.broker.read
        replies = [CredentialError("unavailable"), CredentialError("unavailable")]

        def flaky(component):
            if replies:
                raise replies.pop(0)
            return original(component)

        clock = iter(range(0, 1000, 2))
        with patch.object(secrets, "read_record", side_effect=flaky), patch.object(tern, "monotonic", side_effect=lambda: next(clock)):
            tern.web_token_write(self.runtime / "t" / "web-token", wait=30)
        self.assertTrue((self.runtime / "t" / "web-token").exists())

    def test_write_gives_up_when_the_broker_never_answers(self):
        clock = iter(range(0, 1000, 10))
        with patch.object(secrets, "read_record", side_effect=CredentialError("unavailable")), \
                patch.object(tern, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(CredentialError):
                tern.web_token_write(self.runtime / "t" / "web-token", wait=30)

    def test_generate_keeps_the_rest_of_the_nginx_record_and_refuses_to_overwrite(self):
        self.broker.put("nginx", {"tls.crt": "certificate", "tls.key": "key"})
        with patch.object(secrets, "validate_fields"), redirect_stdout(io.StringIO()):
            tern.web_token_generate(rotate=False)
            first = self.broker.fields("nginx")
            self.assertEqual((first["tls.crt"], first["tls.key"]), ("certificate", "key"))
            self.assertEqual(len(web_token_from_snippet(first["omp-web-token.conf"])), 64)
            with self.assertRaises(tern.ToolError):
                tern.web_token_generate(rotate=False)
            self.assertEqual(self.broker.fields("nginx"), first)
            tern.web_token_generate(rotate=True)
        self.assertNotEqual(self.broker.fields("nginx")["omp-web-token.conf"], first["omp-web-token.conf"])

    def test_the_generated_value_is_accepted_by_the_real_validator(self):
        with patch.object(secrets, "validate_fields"), redirect_stdout(io.StringIO()):
            self.broker.put("nginx", {"tls.crt": "certificate"})
            tern.web_token_generate(rotate=False)
        validate_fields("nginx", {"omp-web-token.conf": self.broker.fields("nginx")["omp-web-token.conf"]}, complete=False)


class OptionalFieldTests(BrokerCase):
    SNIPPET = web_token_snippet(TOKEN)

    def test_the_token_is_optional_but_the_certificates_stay_required(self):
        self.assertNotIn("omp-web-token.conf", required_fields("nginx"))
        self.assertEqual(OPTIONAL["nginx"], {"omp-web-token.conf"})
        with self.assertRaises(ValueError) as error:
            validate_fields("nginx", {"omp-web-token.conf": self.SNIPPET}, complete=True)
        self.assertIn("tls", str(error.exception))

    def test_only_the_exact_snippet_shape_is_accepted(self):
        validate_fields("nginx", {"omp-web-token.conf": self.SNIPPET}, complete=False)
        for bad in ('set $omp_web_token "short";', 'set $omp_web_token "' + "a" * 20 + '"; return 200;',
                    'set $omp_web_token "' + "a" * 20 + '";\nset $x "y";', "x" * 30):
            with self.subTest(bad), self.assertRaises(ValueError):
                validate_fields("nginx", {"omp-web-token.conf": bad}, complete=False)

    def test_empty_optional_means_unset_but_empty_required_is_still_rejected(self):
        validate_fields("nginx", {"omp-web-token.conf": ""}, complete=False)
        with self.assertRaises(ValueError):
            validate_fields("nginx", {"tls.crt": ""}, complete=False)

    def test_stencil_session_token_must_be_a_single_printable_line(self):
        validate_fields("tern", {"stencil-refresh-token": OLD})
        for bad in ("short", OLD + "\n", "has space " + OLD):
            with self.subTest(bad), self.assertRaises(ValueError):
                validate_fields("tern", {"stencil-refresh-token": bad})

    def prepare(self, fields):
        self.broker.put("nginx", fields)
        with patch.object(secrets, "validate_fields"), patch.object(sys, "argv", ["homelab-secrets", "prepare", "nginx"]), \
                redirect_stdout(io.StringIO()):
            secrets.main()
        return self.runtime / "homelab-secrets" / "nginx" / "omp-web-token.conf"

    def test_prepare_delivers_the_snippet_privately(self):
        path = self.prepare({"tls.crt": "c", "tls.key": "k", "tls-public.crt": "c", "tls-public.key": "k", "omp-web-token.conf": self.SNIPPET})
        self.assertEqual(path.read_text(), self.SNIPPET)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_prepare_without_the_token_still_starts_and_withdraws_an_old_delivery(self):
        certificates = {"tls.crt": "c", "tls.key": "k", "tls-public.crt": "c", "tls-public.key": "k"}
        path = self.prepare({**certificates, "omp-web-token.conf": self.SNIPPET})
        self.assertTrue(path.exists())
        path = self.prepare(certificates)
        self.assertFalse(path.exists())
        self.assertTrue((path.parent / "tls.crt").exists())

    def test_creds_drops_unset_optional_fields_but_never_required_ones(self):
        creds = load_module("creds_under_test", "creds.py")
        self.assertEqual(creds.without_unset_optional("nginx", {"tls.crt": "c", "omp-web-token.conf": ""}), {"tls.crt": "c"})
        self.assertEqual(creds.without_unset_optional("nginx", {"tls.crt": "", "omp-web-token.conf": self.SNIPPET}),
                         {"tls.crt": "", "omp-web-token.conf": self.SNIPPET})
        self.assertEqual(creds.missing_fields("nginx", {"tls.crt": "c"}), ["tls-public.crt", "tls-public.key", "tls.key"])


if __name__ == "__main__":
    unittest.main()
