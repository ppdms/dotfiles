import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import principals


class PrincipalIsolationTests(unittest.TestCase):
    def test_same_uid_execution_does_not_inherit_operator_secrets_or_bus(self):
        dangerous = {"GH_TOKEN": "fixture-token", "SSH_AUTH_SOCK": "/operator/ssh", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/operator/bus", "REGISTRY_AUTH_FILE": "/operator/auth.json"}
        with tempfile.TemporaryDirectory() as home:
            role = {"uid": os.getuid(), "account": "homelab-app", "home": home}
            with patch.object(principals, "principal", return_value=role), \
                 patch.dict(os.environ, {**dangerous, "HOMELAB_ENV": shutil.which("env")}):
                result = principals.run_as("app", [sys.executable, "-c", "import json,os; print(json.dumps({'cwd':os.getcwd(),'env':dict(os.environ)}))"],
                                           check=True, capture_output=True, text=True)
        actual = json.loads(result.stdout)
        self.assertEqual(actual["cwd"], role["home"])
        actual = actual["env"]
        self.assertEqual(actual["HOME"], role["home"])
        self.assertEqual(actual["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")
        self.assertEqual(actual["USER"], "homelab-app")
        self.assertFalse(set(dangerous) & set(actual))
        self.assertNotIn("HOMELAB_ENV", actual)

    def test_inherited_environment_override_and_relative_executable_are_rejected(self):
        with self.assertRaises(principals.PrincipalError):
            principals.run_as("app", [sys.executable], env={"GH_TOKEN": "fixture"})
        with patch.object(principals, "principal", return_value={"uid": os.getuid()}):
            with self.assertRaises(principals.PrincipalError):
                principals.command_as("app", ["podman", "ps"])

    def test_user_writable_manifest_cannot_select_dispatch_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "principals.json"
            path.write_text(json.dumps({"principals": {}}))
            with patch.dict(os.environ, {"HOMELAB_PRINCIPALS": str(path)}):
                with self.assertRaises(principals.PrincipalError):
                    principals.manifest()


class ImageStreamingTests(unittest.TestCase):
    reference = "docker.io/grafana/alloy@sha256:" + "1" * 64

    def test_registry_references_and_source_are_checked_before_dispatch(self):
        for source, image in [
            ("operator", self.reference),
            ("image-updates", "docker.io/grafana/alloy:latest"),
            ("image-updates", "docker.io/grafana/alloy@sha256:1"),
            ("image-updates", "docker.io/grafana/alloy@sha256:" + "G" * 64),
            ("image-updates", "docker.io/grafana/alloy@sha256:" + "1" * 64 + "\n"),
            ("image-updates", "docker.io/grafana/alloy@sha512:" + "1" * 64),
        ]:
            with self.subTest(source=source, image=image), \
                 patch.object(principals, "runtime_image_id") as identity, \
                 patch.object(principals, "command_as") as dispatch:
                with self.assertRaises(principals.PrincipalError):
                    principals.stream_image(source, "target", image)
                identity.assert_not_called()
                dispatch.assert_not_called()

    def test_failed_producer_is_not_accepted_even_if_load_exits_successfully(self):
        for source, image in [("source", "localhost/app:committed"),
                              ("image-updates", self.reference)]:
            def dispatch(role, argv):
                script = ("import sys; sys.stdout.buffer.write(b'partial'); sys.exit(7)" if role == source
                          else "import sys; sys.stdin.buffer.read()")
                return [sys.executable, "-c", script]
            with self.subTest(source=source), \
                 patch.object(principals, "principal", return_value={"home": "/fixture"}), \
                 patch.object(principals, "runtime_image_id", return_value="sha256:fixture") as identity, \
                 patch.object(principals, "command_as", side_effect=dispatch):
                with self.assertRaises(principals.PrincipalError):
                    principals.stream_image(source, "target", image)
                self.assertEqual(identity.call_args_list, [] if source == "image-updates"
                                 else [call(source, image)])

    def test_failed_consumer_is_rejected_without_stderr_payloads(self):
        for source, image in [("source", "localhost/app:committed"),
                              ("image-updates", self.reference)]:
            def dispatch(role, argv):
                script = ("import sys; sys.stderr.write('fixture-secret'); sys.exit(0)" if role == source
                          else "import sys; sys.stdin.buffer.read(); sys.stderr.write('fixture-secret'); sys.exit(9)")
                return [sys.executable, "-c", script]
            with self.subTest(source=source), \
                 patch.object(principals, "principal", return_value={"home": "/fixture"}), \
                 patch.object(principals, "runtime_image_id", return_value="sha256:fixture") as identity, \
                 patch.object(principals, "command_as", side_effect=dispatch):
                with self.assertRaises(principals.PrincipalError) as error:
                    principals.stream_image(source, "target", image)
                self.assertNotIn("fixture-secret", str(error.exception))
                self.assertEqual(identity.call_args_list, [] if source == "image-updates"
                                 else [call(source, image)])

    def test_changed_loaded_identity_is_rejected(self):
        def dispatch(role, argv):
            return [sys.executable, "-c", "import sys; sys.stdin.buffer.read()" if role == "target" else ""]
        with patch.object(principals, "runtime_image_id", side_effect=["sha256:original", "sha256:changed"]) as identity, \
             patch.object(principals, "command_as", side_effect=dispatch):
            with self.assertRaises(principals.PrincipalError):
                principals.stream_image("source", "target", "localhost/app:committed")
        self.assertEqual(identity.call_args_list, [
            call("source", "localhost/app:committed"),
            call("target", "sha256:original"),
        ])

    def test_missing_registry_archive_cannot_be_accepted_by_successful_load(self):
        with tempfile.TemporaryDirectory() as home:
            role = {"uid": os.getuid(), "account": "homelab-image-updates", "home": home}
            clean_dispatch = principals.command_as
            def dispatch(name, argv):
                if name == "target":
                    argv = [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]
                return clean_dispatch(name, argv)
            with patch.object(principals, "principal", return_value=role), \
                 patch.dict(os.environ, {"GH_TOKEN": "fixture-token", "HOMELAB_ENV": shutil.which("env"),
                                         "HOMELAB_CAT": shutil.which("cat")}), \
                 patch.object(principals, "command_as", side_effect=dispatch), \
                 patch.object(principals, "runtime_image_id") as identity:
                with self.assertRaises(principals.PrincipalError):
                    principals.stream_image("image-updates", "target", self.reference)
                identity.assert_not_called()

    def test_loaded_registry_archive_without_exact_reference_is_rejected(self):
        def dispatch(role, argv):
            return [sys.executable, "-c", "import sys; sys.stdin.buffer.read()" if role == "target" else ""]
        with patch.object(principals, "principal", return_value={"home": "/fixture"}), \
             patch.object(principals, "command_as", side_effect=dispatch), \
             patch.object(principals, "run_as", return_value=Mock(returncode=1)) as inspect:
            with self.assertRaises(principals.PrincipalError):
                principals.stream_image("image-updates", "target", self.reference)
        self.assertEqual(inspect.call_args.args[0], "target")
        self.assertEqual(inspect.call_args.args[1][-1], self.reference)


if __name__ == "__main__":
    unittest.main()
