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
import tree_image_inputs
from test_tree_image_inputs import ScriptedRegistry, artifact

spec = importlib.util.spec_from_file_location("tree_input_cli", SOURCES / "image-update.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)

REPOSITORY = "docker.io/example/base"
DECLARED = REPOSITORY + ":1"
DOCKERFILE = f"FROM {DECLARED} AS api\n".encode()
NOW = 10 * MINIMUM_AGE


class TreeInputCLITests(unittest.TestCase):
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
                "tree-inputs",
                "--dockerfile-stdin",
                "--json",
            ]
            if require_aged:
                arguments.append("--require-aged")
            with (
                patch.object(updater, "principal", return_value={"uid": os.getuid()}),
                patch.object(tree_image_inputs, "Registry", return_value=registry),
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


if __name__ == "__main__":
    unittest.main()
