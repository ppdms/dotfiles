"""Exercise the CLI's machine-readable boundary with an offline OCI transport."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCES = Path(os.environ.get("HOMELAB_TEST_TOOLS", Path(__file__).parent))
sys.path.insert(0, str(SOURCES))

from image_age import MINIMUM_AGE
import dockerfile_inputs
import dockerfile_pins
from test_dockerfile_inputs import ScriptedRegistry, artifact
from test_dockerfile_pins import HUB, NOW as PIN_NOW, Hub

spec = importlib.util.spec_from_file_location("inputs_cli", SOURCES / "image-update.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)

REPOSITORY = "docker.io/example/base"
DECLARED = REPOSITORY + ":1"
DOCKERFILE = f"FROM {DECLARED} AS api\n".encode()
NOW = 10 * MINIMUM_AGE


class InputsCLITests(unittest.TestCase):
    def invoke(self, require_aged, published_at):
        image = artifact(REPOSITORY, 1)
        if published_at is not None:
            image["publication"] = {
                "source": "docker-hub",
                "repository": REPOSITORY,
                "manifest": image["manifest"],
                "config": image["config"],
                "layers": image["layers"],
                "published_at": published_at,
            }
        registry = ScriptedRegistry({(REPOSITORY, "1"): image})
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            arguments = [
                "--state-dir",
                str(Path(directory) / "state"),
                "inputs",
                "--dockerfile-stdin",
                "--json",
            ]
            if require_aged:
                arguments.append("--require-aged")
            with (
                patch.object(updater, "principal", return_value={"uid": os.getuid()}),
                patch.object(dockerfile_inputs, "Registry", return_value=registry),
                patch("image_age.time.time", return_value=NOW),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}),
                patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(DOCKERFILE))),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = updater.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_observation_and_authorization_each_emit_one_consumable_json_document(self):
        for require_aged, publication in ((False, None), (True, NOW - MINIMUM_AGE)):
            with self.subTest(require_aged=require_aged):
                code, stdout, _ = self.invoke(require_aged, publication)
                self.assertEqual(code, 0)
                response = json.loads(stdout)
                specification = response["specification"]
                expected = REPOSITORY + "@sha256:" + f"{1:064x}"
                self.assertEqual(specification["images"][DECLARED]["reference"], expected)
                self.assertEqual(
                    specification["dockerfile_digest"],
                    "sha256:" + hashlib.sha256(DOCKERFILE).hexdigest(),
                )
                self.assertEqual(response["rendered"], f"FROM {expected} AS api\n")

    def test_ineligible_base_fails_without_publishing_a_json_success(self):
        code, stdout, _ = self.invoke(True, NOW)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")


class PinCLITests(unittest.TestCase):
    def test_pin_emits_one_json_proposal_the_gate_accepts(self):
        dockerfile = b"FROM node:24-alpine3.24 AS client\nFROM alpine:3.24\n"
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(updater, "principal", return_value={"uid": os.getuid()}),
                patch.object(dockerfile_pins, "Registry", return_value=Hub(HUB)),
                patch("image_age.time.time", return_value=PIN_NOW),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}),
                patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(dockerfile))),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = updater.main(["--state-dir", str(Path(directory) / "state"), "pin", "--dockerfile-stdin", "--json"])
        self.assertEqual(code, 0, stderr.getvalue())
        proposal = json.loads(stdout.getvalue())
        self.assertTrue(proposal["ok"], proposal["error"])
        self.assertEqual(proposal["dockerfile_digest"], "sha256:" + hashlib.sha256(dockerfile).hexdigest())
        self.assertEqual(
            proposal["rewritten"],
            "FROM docker.io/library/node:24.21.0-alpine3.24 AS client\nFROM docker.io/library/alpine:3.24.2\n",
        )


class ObserveBuildsTests(unittest.TestCase):
    SECOND = "docker.io/example/other"

    def run_spec(self, entries, registry, trusted=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specification = root / "builds.json"
            specification.write_text(json.dumps({"schema": 1, "images": entries(root)}))
            state = root / "state"
            patches = [
                patch.object(updater, "principal", return_value={"uid": os.getuid()}),
                patch.object(dockerfile_inputs, "Registry", return_value=registry),
                patch("image_age.time.time", return_value=NOW),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ]
            if trusted:
                patches.append(patch.object(updater, "trusted_spec_path", side_effect=lambda path: path))
            for item in patches:
                item.__enter__()
            try:
                code = updater.main(["--state-dir", str(state), "observe-builds", "--spec", str(specification)])
            finally:
                for item in reversed(patches):
                    item.__exit__(None, None, None)
            observed = json.loads((state / "observations.json").read_text()) if (state / "observations.json").exists() else None
        return code, stderr.getvalue(), observed

    def dockerfile(self, root, name, text):
        path = root / name
        path.write_text(text)
        return str(path)

    def test_missing_dockerfile_is_skipped_and_others_are_observed(self):
        registry = ScriptedRegistry({(REPOSITORY, "1"): artifact(REPOSITORY, 1)})
        code, stderr, observed = self.run_spec(lambda root: [
            {"name": "absent", "dockerfile": str(root / "missing/Dockerfile")},
            {"name": "present", "dockerfile": self.dockerfile(root, "Dockerfile", f"FROM {DECLARED}\n")},
        ], registry)
        self.assertEqual(code, 0)
        self.assertIn("absent: skipped", stderr)
        self.assertIn(DECLARED, observed["build_inputs"])

    def test_one_failing_image_does_not_stop_the_rest_but_fails_the_run(self):
        registry = ScriptedRegistry({(REPOSITORY, "1"): artifact(REPOSITORY, 1)})
        code, stderr, observed = self.run_spec(lambda root: [
            {"name": "broken", "dockerfile": self.dockerfile(root, "Broken", "FROM $BASE\n")},
            {"name": "unavailable", "dockerfile": self.dockerfile(root, "Unavailable", f"FROM {self.SECOND}:1\n")},
            {"name": "healthy", "dockerfile": self.dockerfile(root, "Healthy", f"FROM {DECLARED}\n")},
        ], registry)
        self.assertEqual(code, 1)
        self.assertIn("broken, unavailable", stderr)
        self.assertNotIn("healthy,", stderr)
        self.assertIn(DECLARED, observed["build_inputs"])

    def test_untrusted_specification_path_is_rejected_before_any_observation(self):
        registry = ScriptedRegistry({(REPOSITORY, "1"): artifact(REPOSITORY, 1)})
        code, stderr, observed = self.run_spec(lambda root: [
            {"name": "present", "dockerfile": self.dockerfile(root, "Dockerfile", f"FROM {DECLARED}\n")},
        ], registry, trusted=False)
        self.assertEqual(code, 1)
        self.assertIn("Nix store", stderr)
        self.assertIsNone(observed)

    def test_malformed_specification_is_rejected(self):
        registry = ScriptedRegistry({})
        code, stderr, observed = self.run_spec(lambda root: [{"name": "relative", "dockerfile": "Dockerfile"}], registry)
        self.assertEqual(code, 1)
        self.assertIn("invalid build specification entry", stderr)
        self.assertIsNone(observed)


if __name__ == "__main__":
    unittest.main()
