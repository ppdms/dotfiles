"""Consumer-visible validation and private-input projection boundaries."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("profile_helper", ROOT / "tools/profile/profile.py")
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class ProfileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads((ROOT / "examples/macbook/profile.json").read_text())
        self.contract = profile.Contract()

    def test_unselected_private_payload_is_not_traversed_or_projected(self):
        marker = "AGE-SECRET-KEY-PRIVATE-BOUNDARY-MARKER"
        self.source["profiles"]["nixos"] = {"password": marker}
        self.source["files"] = {"vscodeSshConfig": marker}
        self.source["wireguard"] = {"private_key": marker}
        projected = self.contract.project(self.source, "darwin")
        self.assertEqual(set(projected), {"profiles"})
        self.assertEqual(set(projected["profiles"]), {"darwin"})
        self.assertNotIn(marker, json.dumps(projected))

    def test_selected_credentials_fail_without_exposing_name_or_value(self):
        secret_name = "unexpected_password_private_marker"
        secret_value = "DO-NOT-DISPLAY-CREDENTIAL-VALUE"
        self.source["profiles"]["darwin"]["user"][secret_name] = secret_value
        with self.assertRaises(profile.ProfileError) as failure:
            self.contract.project(self.source, "darwin")
        self.assertNotIn(secret_name, str(failure.exception))
        self.assertNotIn(secret_value, str(failure.exception))

    def test_private_key_cannot_be_disguised_as_identity_text(self):
        self.source["profiles"]["darwin"]["user"]["fullName"] = "AGE-SECRET-KEY-PRIVATE-MARKER"
        with self.assertRaisesRegex(profile.ProfileError, "private-key material"):
            self.contract.project(self.source, "darwin")

    def test_darwin_compatibility_is_not_a_release_or_boolean(self):
        for version in ("25.05", True, 0, 7):
            with self.subTest(version=version):
                source = copy.deepcopy(self.source)
                source["profiles"]["darwin"]["system"]["systemStateVersion"] = version
                with self.assertRaises(profile.ProfileError):
                    self.contract.project(source, "darwin")

    def test_darwin_dns_preserves_ipv4_and_ipv6_literals(self):
        addresses = [
            "192.0.2.1", "2620:fe::fe", "fd00:dead:beef::2",
            "::", "::1", "2001:DB8:0:1:2:3:4:5", "2001:db8::",
            "::ffff:192.0.2.1", "2001:db8:0:0:0:0:192.0.2.1",
            "2001:db8::192.0.2.1",
        ]
        self.source["profiles"]["darwin"]["network"] = {
            "modes": {"default": {"dnsServers": addresses}}
        }
        projected = self.contract.project(self.source, "darwin")
        self.assertEqual(
            projected["profiles"]["darwin"]["network"]["modes"]["default"]["dnsServers"],
            addresses,
        )

    def test_darwin_dns_rejects_malformed_or_nonliteral_addresses(self):
        for address in (
            "256.0.0.1", "01.2.3.4", "dns.example.net", "2001:db8::1/64",
            "fe80::1%en0", "1:2:3:4:5:6:7", "1:2:3:4:5:6:7:8:9",
            "1:2:3:4:5:6:7::8", "2001::db8::1", "2001:db8::zz",
            "12345::", "::ffff:192.0.2.256",
        ):
            with self.subTest(address=address):
                self.source["profiles"]["darwin"]["network"] = {
                    "modes": {"default": {"dnsServers": [address]}}
                }
                with self.assertRaises(profile.ProfileError):
                    self.contract.project(self.source, "darwin")

    def test_codex_omitted_object_or_policy_and_null_are_unmanaged(self):
        for codex in (None, {}, {"sandboxMode": None}):
            with self.subTest(codex=codex):
                source = copy.deepcopy(self.source)
                applications = source["profiles"]["darwin"]["applications"]
                if codex is None:
                    applications.pop("codex", None)
                else:
                    applications["codex"] = codex
                projected = self.contract.project(source, "darwin")
                self.assertIsNone(projected["profiles"]["darwin"]["applications"]["codex"]["sandboxMode"])

    def test_codex_policy_rejects_unknown_modes_and_wrong_types(self):
        for mode in ("full-access", "", True, 1, {}, []):
            with self.subTest(mode=mode):
                self.source["profiles"]["darwin"]["applications"]["codex"]["sandboxMode"] = mode
                with self.assertRaises(profile.ProfileError):
                    self.contract.project(self.source, "darwin")

    def test_inline_backup_credentials_are_not_metadata(self):
        source = json.loads((ROOT / "examples/server/profile.json").read_text())
        source["profiles"]["nixos"]["homelab"]["backup"]["repositoryUri"] = "https://user:secret@backup.example.net"
        with self.assertRaisesRegex(profile.ProfileError, "inline URI credentials"):
            self.contract.project(source, "nixos")


class BackrestPrivateFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args = SimpleNamespace(state_dir=self.root / "state",
                                    backrest_config=self.root / "runtime/config.json",
                                    platform="darwin", bootstrap=None)
        self.source = json.loads((ROOT / "examples/macbook/profile.json").read_text())
        self.source["files"] = {"vscodeSshConfig": "Host fixture\n",
                                "syncthingConfig": "<configuration/>\n",
                                "unrelated": "keep this private file"}
        self.source["wireguard"] = {"private_key": "keep this private key"}
        self.raw = '{\n  "password": "BACKREST-PRIVATE-FIXTURE", "unicode": "α"\n}\n'

    def write_runtime(self, data=None):
        profile.atomic_file(self.args.backrest_config, self.raw.encode() if data is None else data)

    def test_capture_preserves_raw_local_edits_and_other_private_state(self):
        before = copy.deepcopy(self.source)
        self.write_runtime()
        profile.capture_backrest(self.source, self.args)
        self.assertEqual(profile.backrest_payload(self.source), self.raw.encode())
        for key in before:
            if key != "files":
                self.assertEqual(self.source[key], before[key])
        for key, value in before["files"].items():
            self.assertEqual(self.source["files"][key], value)
        projected = profile.Contract().project(self.source, "darwin")
        self.assertNotIn("BACKREST-PRIVATE-FIXTURE", json.dumps(projected))
        self.assertEqual(set(projected), {"profiles"})

    def test_absent_runtime_preserves_existing_optional_payload(self):
        self.source["files"]["backrestConfig"] = self.raw
        profile.capture_backrest(self.source, self.args)
        self.assertEqual(profile.backrest_payload(self.source), self.raw.encode())
        del self.source["files"]["backrestConfig"]
        profile.capture_backrest(self.source, self.args)
        self.assertNotIn("backrestConfig", self.source["files"])
        self.assertIsNone(profile.backrest_payload(self.source))

    def test_invalid_runtime_json_or_encoding_is_rejected_without_secret(self):
        for invalid in (b'{"password":"PRIVATE-INVALID-SECRET"',
                        b'{"password":"PRIVATE-INVALID-SECRET", "password":"duplicate"}',
                        b'{"password":"PRIVATE-INVALID-SECRET\xff"}'):
            with self.subTest(payload=invalid):
                self.write_runtime(invalid)
                with self.assertRaises(profile.ProfileError) as failure:
                    profile.capture_backrest(copy.deepcopy(self.source), self.args)
                self.assertNotIn("PRIVATE-INVALID-SECRET", str(failure.exception))

    def test_invalid_embedded_secret_payload_is_rejected(self):
        for value in ({"password": "SECRET"}, '{"password":"SECRET"', "\udcff"):
            with self.subTest(payload=value):
                self.source["files"]["backrestConfig"] = value
                with self.assertRaises(profile.ProfileError) as failure:
                    profile.backrest_payload(self.source)
                self.assertNotIn("SECRET", str(failure.exception))

    def test_runtime_symlink_and_nonregular_targets_are_rejected(self):
        self.write_runtime()
        target = self.root / "untouched.json"
        self.args.backrest_config.rename(target)
        self.args.backrest_config.symlink_to(target)
        with self.assertRaises(profile.ProfileError):
            profile.capture_backrest(self.source, self.args)
        self.assertEqual(target.read_bytes(), self.raw.encode())
        self.args.backrest_config.unlink()
        os.mkfifo(self.args.backrest_config)
        with self.assertRaises(profile.ProfileError):
            profile.capture_backrest(self.source, self.args)

    def test_runtime_path_defaults_are_independent_of_profile_state(self):
        self.args.backrest_config = None
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg"),
                                     "NIX_CONFIG_BACKREST_CONFIG": ""}):
            self.assertEqual(profile.backrest_config_path(self.args),
                             self.root / "xdg/backrest/config.json")
            os.environ["NIX_CONFIG_BACKREST_CONFIG"] = str(self.root / "override.json")
            self.assertEqual(profile.backrest_config_path(self.args), self.root / "override.json")
            self.args.backrest_config = self.root / "explicit.json"
            self.assertEqual(profile.backrest_config_path(self.args), self.root / "explicit.json")
        self.args.backrest_config = None
        with patch.dict(os.environ, {"XDG_STATE_HOME": "", "NIX_CONFIG_BACKREST_CONFIG": ""}):
            self.assertEqual(profile.backrest_config_path(self.args),
                             Path.home() / ".local/state/backrest/config.json")

    def test_runtime_override_cannot_write_nix_projection_or_store(self):
        for path in (self.args.state_dir / ".profile-inputs/config.json",
                     Path("/nix/store/fixture/config.json")):
            self.args.backrest_config = path
            with self.assertRaisesRegex(profile.ProfileError, "outside Nix"):
                profile.backrest_config_path(self.args)

    def test_inaccessible_regular_file_fails_privileged_read_without_plaintext(self):
        # Exercise actual filesystem permissions and a real missing-sudo failure,
        # not a fabricated subprocess response. Root drops privilege for this read.
        isolated_helper = self.root / "profile.py"
        shutil.copyfile(ROOT / "tools/profile/profile.py", isolated_helper)
        isolated_helper.chmod(0o644)
        self.root.chmod(0o755)
        target = self.root / "root-only.json"
        target.write_text(self.raw)
        target.chmod(0)
        kwargs = {}
        if os.geteuid() == 0:
            import pwd
            try:
                user = pwd.getpwnam("nobody")
            except KeyError:
                self.skipTest("No unprivileged fixture account is available")
            kwargs = {"user": user.pw_uid, "group": user.pw_gid, "extra_groups": []}
        code = (
            "import importlib.util, pathlib, sys\n"
            "spec=importlib.util.spec_from_file_location('fixture',sys.argv[1])\n"
            "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n"
            "try: module.read_backrest_config(pathlib.Path(sys.argv[2]))\n"
            "except module.ProfileError as error: print(error);sys.exit(7)\n"
        )
        result = subprocess.run([sys.executable, "-c", code, str(isolated_helper), str(target)],
                                env={**os.environ, "PATH": ""}, capture_output=True, **kwargs)
        self.assertEqual(result.returncode, 7, result.stderr.decode())
        self.assertIn(b"privileged Backrest config read", result.stdout)
        self.assertNotIn(b"BACKREST-PRIVATE-FIXTURE", result.stdout + result.stderr)


@unittest.skipUnless(shutil.which("sops") and shutil.which("age-keygen"),
                     "Real sops and age-keygen are required for encrypted fixture roundtrips")
class BackrestEncryptedRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state with spaces"
        self.state.mkdir(mode=0o700)
        self.runtime = self.root / "backrest/config.json"
        self.identity = self.root / "identity with spaces.txt"
        self.run_process(["age-keygen", "-o", str(self.identity)])
        recipient = self.run_process(["age-keygen", "-y", str(self.identity)]).stdout.decode().strip()
        self.policy = self.state / ".sops.yaml"
        self.policy.write_text(json.dumps({"creation_rules": [{"path_regex": ".*", "age": recipient}]}))
        self.policy.chmod(0o600)
        for filename, kind, data in (
                ("secrets.sops.yaml", "json", b'{"github_token":"FIXTURE-OTHER-PRIVATE-SECRET"}'),
                ("rclone.ini", "ini", b"[fixture]\ntype = s3\nsecret_access_key = FIXTURE-RCLONE-SECRET\n")):
            encrypted = self.run_process(["sops", "--encrypt", "--config", str(self.policy),
                                          "--input-type", kind, "--output-type", "yaml" if kind == "json" else "ini",
                                          "--filename-override", filename, "/dev/stdin"], data).stdout
            profile.atomic_file(self.state / filename, encrypted)
        self.source = json.loads((ROOT / "examples/macbook/profile.json").read_text())
        self.source["files"] = {"vscodeSshConfig": "Host fixture\n",
                                "syncthingConfig": "<configuration/>\n",
                                "unrelated": "FIXTURE-PRIVATE-LOCAL-FILE"}
        self.source["wireguard"] = {"private_key": "FIXTURE-PRIVATE-WIREGUARD-KEY"}
        self.bootstrap = self.root / "bootstrap.json"
        profile.atomic_file(self.bootstrap, profile.json_bytes(self.source))
        self.raw = '{\n "password": "BACKREST-ENCRYPTED-FIXTURE", "repos": []\n}\n'
        profile.atomic_file(self.runtime, self.raw.encode())

    def run_process(self, argv, data=None):
        result = subprocess.run(argv, input=data, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result

    def cli(self, operation, *extra, success=True, env=None, use_state_dir=True, status=None):
        state_args = ["--state-dir", str(self.state)] if use_state_dir else []
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/profile/profile.py"), operation, "--platform", "darwin",
             *state_args, "--backrest-config", str(self.runtime),
             "--identity", str(self.identity), *map(str, extra)],
            capture_output=True, env={**os.environ, **(env or {})})
        self.assertEqual(result.returncode, status if status is not None else (0 if success else 1),
                         result.stderr.decode())
        return result

    def start_roundtrip(self):
        self.cli("seal", "--bootstrap", self.bootstrap)
        self.assertNotIn(self.raw.encode(), (self.state / "profile.sops.yaml").read_bytes())
        self.runtime.unlink()
        self.cli("render")
        self.assertEqual(self.runtime.read_bytes(), self.raw.encode())

    def assert_projection_excludes_runtime(self):
        selected = (self.state / "input").resolve()
        self.assertEqual({path.name for path in selected.iterdir()},
                         {"profile.json", "flake.nix", "secrets.sops.yaml", "rclone.ini"})
        projected = json.loads((selected / "profile.json").read_bytes())
        self.assertEqual(set(projected), {"profiles"})
        self.assertEqual(set(projected["profiles"]), {"darwin"})
        for path in (self.state / ".profile-inputs").rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(b"BACKREST-ENCRYPTED-FIXTURE", data)
                self.assertNotIn(b"BACKREST-LOCAL-EDIT", data)

    def editor_command(self, mode="success", *arguments):
        editor = self.root / "fixture editor.py"
        editor.write_text(
            "#!" + sys.executable + "\n"
            "import fcntl, json, pathlib, sys\n"
            "mode = sys.argv[1] if len(sys.argv) > 2 else 'success'\n"
            "path = pathlib.Path(sys.argv[-1])\n"
            "with path.with_name('.profile.lock').open('a') as lock:\n"
            "    try:\n"
            "        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "        locked = False\n"
            "    except BlockingIOError:\n"
            "        locked = True\n"
            "path.with_name('editor-observation.json').write_text(json.dumps({\n"
            "    'path': str(path), 'arguments': sys.argv[2:-1], 'locked': locked}))\n"
            "source = json.loads(path.read_bytes())\n"
            "source['profiles']['darwin']['system']['hostname'] = 'edited-laptop'\n"
            "if mode == 'invalid':\n"
            "    source['profiles']['darwin']['user']['password'] = 'EDIT-PRIVATE-MARKER'\n"
            "path.write_text(json.dumps(source) + '\\n')\n"
            "if mode == 'failure':\n"
            "    print('EDIT-PRIVATE-MARKER', file=sys.stderr)\n"
            "    sys.exit(9)\n"
        )
        editor.chmod(0o700)
        return shlex.join([sys.executable, str(editor), mode, *arguments])

    def test_edit_state_precedence_preserves_selected_path_and_identity(self):
        identity = self.identity.read_bytes()
        fixture_files = {name: (self.state / name).read_bytes()
                         for name in (".sops.yaml", "secrets.sops.yaml", "rclone.ini")}
        command = self.editor_command()
        for choice in ("home", "xdg", "environment", "explicit"):
            with self.subTest(choice=choice):
                case = self.root / choice
                home = case / "home with spaces"
                xdg = case / "xdg state with spaces"
                override = case / "override with spaces"
                explicit = case / "explicit with spaces"
                env = {"HOME": str(home), "XDG_STATE_HOME": "",
                       "NIX_CONFIG_STATE_DIR": "", "VISUAL": "", "EDITOR": command}
                selected = home / ".local/state/nix-config"
                extra = []
                if choice in ("xdg", "environment", "explicit"):
                    env["XDG_STATE_HOME"] = str(xdg)
                    selected = xdg / "nix-config"
                if choice in ("environment", "explicit"):
                    env["NIX_CONFIG_STATE_DIR"] = str(override)
                    selected = override
                if choice == "explicit":
                    extra = ["--state-dir", explicit]
                    selected = explicit
                for name, data in fixture_files.items():
                    profile.atomic_file(selected / name, data)
                self.cli("seal", "--bootstrap", self.bootstrap, *extra,
                         env=env, use_state_dir=False)
                self.cli("render", *extra, env=env, use_state_dir=False)
                original_ciphertext = (selected / "profile.sops.yaml").read_bytes()
                result = self.cli("edit", *extra, env=env, use_state_dir=False)
                self.assertEqual(result.stdout.strip(), str(selected / "profile.sops.yaml").encode())
                observation = json.loads((selected / "editor-observation.json").read_bytes())
                self.assertEqual(observation["path"], str(selected / "profile.json"))
                self.assertTrue(observation["locked"])
                self.assertNotEqual((selected / "profile.sops.yaml").read_bytes(), original_ciphertext)
                (selected / "profile.json").unlink()
                self.cli("render", *extra, env=env, use_state_dir=False)
                metadata = json.loads((selected / "profile.json").read_bytes())
                self.assertEqual(metadata["profiles"]["darwin"]["system"]["hostname"], "edited-laptop")
                self.assertEqual(self.identity.read_bytes(), identity)
                for unselected in (home / ".local/state/nix-config", xdg / "nix-config",
                                   override, explicit):
                    if unselected != selected:
                        self.assertFalse(unselected.exists())

    def test_edit_visual_precedes_editor_and_arguments_are_not_shell_text(self):
        self.start_roundtrip()
        marker = self.root / "shell injection marker"
        command = self.editor_command("success", "--wait", "two words")
        # These unquoted shell operators remain ordinary argv elements.
        command += " ; touch " + shlex.quote(str(marker)) + " $(false)"
        self.cli("edit", env={"VISUAL": command, "EDITOR": self.editor_command("failure")})
        observation = json.loads((self.state / "editor-observation.json").read_bytes())
        self.assertEqual(observation["arguments"],
                         ["--wait", "two words", ";", "touch", str(marker), "$(false)"])
        self.assertFalse(marker.exists())
        self.cli("render")
        metadata = json.loads((self.state / "profile.json").read_bytes())
        self.assertEqual(metadata["profiles"]["darwin"]["system"]["hostname"], "edited-laptop")
        self.assertEqual(metadata["wireguard"], self.source["wireguard"])
        self.assertEqual(metadata["files"]["unrelated"], self.source["files"]["unrelated"])
        self.assert_projection_excludes_runtime()

    def test_edit_failure_or_invalid_content_retains_editable_data_and_ciphertext(self):
        for mode in ("failure", "invalid"):
            with self.subTest(mode=mode):
                self.start_roundtrip()
                original = (self.state / "profile.sops.yaml").read_bytes()
                result = self.cli("edit", success=False,
                                  env={"VISUAL": "", "EDITOR": self.editor_command(mode)})
                self.assertNotIn(b"EDIT-PRIVATE-MARKER", result.stdout + result.stderr)
                self.assertEqual((self.state / "profile.sops.yaml").read_bytes(), original)
                metadata = json.loads((self.state / "profile.json").read_bytes())
                self.assertEqual(metadata["profiles"]["darwin"]["system"]["hostname"], "edited-laptop")
                if mode == "invalid":
                    self.assertEqual(metadata["profiles"]["darwin"]["user"]["password"],
                                     "EDIT-PRIVATE-MARKER")
                # Failed edits must never publish their changed metadata into Nix.
                projected = json.loads((self.state / "input/profile.json").read_bytes())
                self.assertEqual(projected["profiles"]["darwin"]["system"]["hostname"],
                                 self.source["profiles"]["darwin"]["system"]["hostname"])
                # Retained edits cannot be silently clobbered by another edit.
                retained = (self.state / "profile.json").read_bytes()
                self.cli("edit", success=False,
                         env={"VISUAL": "", "EDITOR": self.editor_command("success")})
                self.assertEqual((self.state / "profile.json").read_bytes(), retained)
                self.assertEqual((self.state / "profile.sops.yaml").read_bytes(), original)
                (self.state / "profile.json").unlink()

    def test_edit_refuses_runtime_backrest_drift_before_editor(self):
        self.start_roundtrip()
        original = (self.state / "profile.sops.yaml").read_bytes()
        metadata = (self.state / "profile.json").read_bytes()
        edited = b'{"password":"BACKREST-LOCAL-EDIT"}\n'
        self.runtime.write_bytes(edited)
        result = self.cli("edit", success=False,
                          env={"VISUAL": "", "EDITOR": self.editor_command()})
        self.assertIn(b"locally changed Backrest config", result.stderr)
        self.assertNotIn(b"BACKREST-LOCAL-EDIT", result.stdout + result.stderr)
        self.assertFalse((self.state / "editor-observation.json").exists())
        self.assertEqual(self.runtime.read_bytes(), edited)
        self.assertEqual((self.state / "profile.json").read_bytes(), metadata)
        self.assertEqual((self.state / "profile.sops.yaml").read_bytes(), original)

    def test_edit_requires_sealed_metadata_and_rejects_bootstrap_or_force(self):
        editor = self.editor_command()
        for option in (("--bootstrap", self.bootstrap), ("--force",)):
            with self.subTest(option=option):
                self.cli("edit", *option, status=2, env={"VISUAL": "", "EDITOR": editor})
        # A valid unsealed file is not a routine fallback for a missing seal.
        profile.atomic_file(self.state / "profile.json", profile.json_bytes(self.source))
        original = (self.state / "profile.json").read_bytes()
        self.cli("edit", success=False, env={"VISUAL": "", "EDITOR": editor})
        self.assertFalse((self.state / "editor-observation.json").exists())
        self.assertFalse((self.state / "profile.sops.yaml").exists())
        self.assertEqual((self.state / "profile.json").read_bytes(), original)

    def test_plaintext_comments_never_enter_projection_or_diagnostics(self):
        self.start_roundtrip()
        previous_input = os.readlink(self.state / "input")
        previous_metadata = (self.state / "profile.json").read_bytes()
        previous_generations = {path.name for path in (self.state / ".profile-inputs").iterdir()}
        marker = b"FIXTURE-PLAINTEXT-COMMENT-PRIVATE"
        yaml_path = self.state / "secrets.sops.yaml"
        ini_path = self.state / "rclone.ini"
        originals = {yaml_path: yaml_path.read_bytes(), ini_path: ini_path.read_bytes()}
        yaml_lines = originals[yaml_path].splitlines(keepends=True)
        ini_lines = originals[ini_path].splitlines(keepends=True)

        def append_inline(lines, index, symbol):
            changed = list(lines)
            changed[index] = changed[index].rstrip(b"\r\n") + b" " + symbol + b" " + marker + b"\n"
            return b"".join(changed)

        yaml_scalar = next(i for i, line in enumerate(yaml_lines) if line.startswith(b"github_token:"))
        yaml_armor = next(i for i, line in enumerate(yaml_lines)
                          if line.lstrip().removeprefix(b"- ").startswith(b"enc: |"))
        yaml_metadata = next(i for i, line in enumerate(yaml_lines) if line.lstrip().startswith(b"version:"))
        ini_scalar = next(i for i, line in enumerate(ini_lines) if line.lstrip().startswith(b"type "))
        ini_section = next(i for i, line in enumerate(ini_lines) if line.startswith(b"[fixture]"))
        ini_metadata = next(i for i, line in enumerate(ini_lines) if line.lstrip().startswith(b"version "))
        attempts = (
            (yaml_path, b"# " + marker + b"\n" + originals[yaml_path]),
            (yaml_path, append_inline(yaml_lines, yaml_scalar, b"#")),
            (yaml_path, append_inline(yaml_lines, yaml_armor, b"#")),
            (yaml_path, append_inline(yaml_lines, yaml_metadata, b"#")),
            (yaml_path, originals[yaml_path] + b"\n# " + marker + b"\n"),
            (ini_path, b"# " + marker + b"\n" + originals[ini_path]),
            (ini_path, b"; " + marker + b"\n" + originals[ini_path]),
            (ini_path, append_inline(ini_lines, ini_scalar, b"#")),
            (ini_path, append_inline(ini_lines, ini_scalar, b";")),
            (ini_path, append_inline(ini_lines, ini_section, b";")),
            (ini_path, append_inline(ini_lines, ini_metadata, b"#")),
        )
        for index, (path, ciphertext) in enumerate(attempts):
            with self.subTest(attempt=index):
                for original_path, data in originals.items():
                    original_path.write_bytes(data)
                path.write_bytes(ciphertext)
                failure = self.cli("render", success=False)
                self.assertNotIn(marker, failure.stdout + failure.stderr)
                self.assertEqual(path.read_bytes(), ciphertext)
                self.assertEqual(os.readlink(self.state / "input"), previous_input)
                self.assertEqual((self.state / "profile.json").read_bytes(), previous_metadata)
                self.assertEqual({path.name for path in (self.state / ".profile-inputs").iterdir()},
                                 previous_generations)
                for projected in (self.state / ".profile-inputs").rglob("*"):
                    if projected.is_file():
                        self.assertNotIn(marker, projected.read_bytes())

    def test_native_sops_empty_ini_values_project_but_nonempty_plaintext_does_not(self):
        encrypted = self.run_process(
            ["sops", "--encrypt", "--config", str(self.policy), "--input-type", "ini",
             "--output-type", "ini", "--filename-override", "rclone.ini", "/dev/stdin"],
            b"[fixture]\ntype = s3\nclient_id =\nsecret_access_key = FIXTURE-RCLONE-SECRET\n",
        ).stdout
        path = self.state / "rclone.ini"
        profile.atomic_file(path, encrypted)
        self.start_roundtrip()
        self.assertEqual((self.state / "input" / "rclone.ini").read_bytes(), encrypted)
        malformed = b"\n".join(
            line + b"PLAINTEXT-PRIVATE-MARKER" if line.startswith(b"client_id") else line
            for line in encrypted.split(b"\n")
        )
        with self.assertRaises(profile.ProfileError) as failure:
            profile.verify_sops(malformed, "rclone.ini", path)
        self.assertNotIn("PLAINTEXT-PRIVATE-MARKER", str(failure.exception))

    def test_real_encrypted_comments_metadata_symbols_and_armor_preserve_exact_source(self):
        policy = json.loads(self.policy.read_text())
        # These symbols are part of quoted native-format metadata, not comments.
        policy["creation_rules"][0]["unencrypted_comment_regex"] = "^https://example.invalid/#metadata;only$"
        self.policy.write_text(json.dumps(policy))
        fixtures = (
            ("secrets.sops.yaml", "yaml",
             b"# FIXTURE-ENCRYPTED-YAML-COMMENT\n"
             b"github_token: FIXTURE-OTHER-PRIVATE-SECRET # FIXTURE-ENCRYPTED-INLINE-COMMENT\n"),
            ("rclone.ini", "ini",
             b"; FIXTURE-ENCRYPTED-INI-SECTION-COMMENT\n"
             b"[fixture]\n"
             b"# FIXTURE-ENCRYPTED-INI-KEY-COMMENT\n"
             b"type = s3\n"
             b"secret_access_key = FIXTURE-RCLONE-SECRET ; FIXTURE-ENCRYPTED-INI-INLINE-COMMENT\n"),
        )
        originals = {}
        for filename, kind, plaintext in fixtures:
            encrypted = self.run_process(
                ["sops", "--encrypt", "--config", str(self.policy), "--input-type", kind,
                 "--output-type", kind, "--filename-override", filename, "/dev/stdin"],
                plaintext).stdout
            self.assertIn(b"type:comment]", encrypted)
            self.assertNotIn(b"FIXTURE-ENCRYPTED-", encrypted)
            path = self.state / filename
            profile.atomic_file(path, encrypted)
            originals[filename] = encrypted
            # Native decryption verifies the real fixture's MAC and comment
            # crypto, independently of the structure-only projection gate.
            decrypted = subprocess.run(
                ["sops", "--decrypt", str(path)], capture_output=True,
                env={**os.environ, "SOPS_AGE_KEY_FILE": str(self.identity)})
            self.assertEqual(decrypted.returncode, 0, decrypted.stderr.decode())
            self.assertIn(b"FIXTURE-ENCRYPTED-", decrypted.stdout)
        self.start_roundtrip()
        selected = (self.state / "input").resolve()
        for filename, original in originals.items():
            self.assertEqual((self.state / filename).read_bytes(), original)
            self.assertEqual((selected / filename).read_bytes(), original)

    def test_seal_render_preserves_edits_private_state_and_narrow_projection(self):
        self.start_roundtrip()
        edited = '{ "password": "BACKREST-LOCAL-EDIT", "repos": ["local"] }\n'
        self.runtime.write_text(edited)
        self.cli("seal")
        self.runtime.unlink()
        self.cli("render")
        self.assertEqual(self.runtime.read_text(), edited)
        metadata = json.loads((self.state / "profile.json").read_bytes())
        self.assertEqual(metadata["files"]["backrestConfig"], edited)
        self.assertEqual(metadata["files"]["unrelated"], self.source["files"]["unrelated"])
        self.assertEqual(metadata["wireguard"], self.source["wireguard"])
        self.assertEqual(metadata["files"]["vscodeSshConfig"], "Host fixture\n")
        self.assertEqual(metadata["files"]["syncthingConfig"], "<configuration/>\n")
        self.assert_projection_excludes_runtime()

    def test_local_drift_refuses_without_clobber_and_force_restores(self):
        self.start_roundtrip()
        previous = os.readlink(self.state / "input")
        metadata = (self.state / "profile.json").read_bytes()
        edited = b'{"password":"BACKREST-LOCAL-EDIT"}\n'
        self.runtime.write_bytes(edited)
        failure = self.cli("render", success=False)
        self.assertIn(b"locally changed Backrest config", failure.stderr)
        self.assertNotIn(b"BACKREST-LOCAL-EDIT", failure.stdout + failure.stderr)
        self.assertEqual(self.runtime.read_bytes(), edited)
        self.assertEqual((self.state / "profile.json").read_bytes(), metadata)
        self.assertEqual(os.readlink(self.state / "input"), previous)
        self.cli("render", "--force")
        self.assertEqual(self.runtime.read_bytes(), self.raw.encode())
        self.assert_projection_excludes_runtime()

    def test_invalid_local_secret_does_not_replace_ciphertext_or_metadata(self):
        self.start_roundtrip()
        previous = {name: (self.state / name).read_bytes()
                    for name in ("profile.json", "profile.sops.yaml")}
        invalid = b'{"password":"BACKREST-INVALID-SECRET"\xff}'
        self.runtime.write_bytes(invalid)
        failure = self.cli("seal", success=False)
        self.assertIn(b"UTF-8 JSON", failure.stderr)
        self.assertNotIn(b"BACKREST-INVALID-SECRET", failure.stdout + failure.stderr)
        self.assertEqual(self.runtime.read_bytes(), invalid)
        for name, data in previous.items():
            self.assertEqual((self.state / name).read_bytes(), data)

    def test_force_still_rejects_symlink_without_publishing(self):
        self.start_roundtrip()
        previous = os.readlink(self.state / "input")
        target = self.root / "untouched.json"
        self.runtime.rename(target)
        self.runtime.symlink_to(target)
        self.cli("render", "--force", success=False)
        self.assertTrue(self.runtime.is_symlink())
        self.assertEqual(target.read_bytes(), self.raw.encode())
        self.assertEqual(os.readlink(self.state / "input"), previous)

    def test_omitted_backrest_is_optional_and_does_not_overwrite_runtime(self):
        self.runtime.unlink()
        self.cli("seal", "--bootstrap", self.bootstrap)
        self.cli("render")
        self.assertFalse(self.runtime.exists())
        profile.atomic_file(self.runtime, self.raw.encode())
        self.cli("render")
        self.assertEqual(self.runtime.read_bytes(), self.raw.encode())
        self.assertNotIn("backrestConfig", json.loads((self.state / "profile.json").read_bytes())["files"])
        self.assert_projection_excludes_runtime()

    def test_conversion_captures_edits_then_seals_and_renders_real_ciphertext(self):
        self.start_roundtrip()
        original_ciphertext = (self.state / "profile.sops.yaml").read_bytes()
        edited = '{ "password": "BACKREST-LOCAL-EDIT", "repos": ["fixture"] }\n'
        self.runtime.write_text(edited)
        checkout = self.root / "checkout"
        checkout.mkdir()
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/profile/profile.py"), "python",
             str(ROOT / "tools/profile/convert-profile.py"), "--source", str(self.state / "profile.json"),
             "--state-dir", str(self.state), "--backrest-config", str(self.runtime),
             "--checkout", str(checkout), "--darwin-state-version", "6",
             "--identity", str(self.identity), "--seal"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        backups = list((self.state / "backups").glob("*.sops.yaml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original_ciphertext)
        metadata = json.loads((self.state / "profile.json").read_bytes())
        self.assertEqual(metadata["files"]["backrestConfig"], edited)
        self.assertEqual(metadata["wireguard"], self.source["wireguard"])
        self.assertEqual(metadata["sourceDarwinProfile"], self.source["profiles"]["darwin"])
        self.assertEqual(metadata["profiles"]["darwin"]["system"]["systemStateVersion"], 6)
        self.runtime.unlink()
        self.cli("render")
        self.assertEqual(self.runtime.read_text(), edited)
        self.assert_projection_excludes_runtime()


class PublishInputReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.args = SimpleNamespace(state_dir=Path(self.temporary.name) / "state", force=False)
        self.files = {"profile.json": b"{}\n", "flake.nix": b"{ }\n"}

    def generations(self):
        root = self.args.state_dir / ".profile-inputs"
        return sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []

    def test_unchanged_files_reuse_generation_and_changed_files_publish_new_one(self):
        profile.publish_input(self.args, self.files)
        first = profile.current_input(self.args)
        profile.publish_input(self.args, dict(self.files))
        self.assertEqual(profile.current_input(self.args), first)
        self.assertEqual(self.generations(), [first])

        profile.publish_input(self.args, {**self.files, "profile.json": b'{"changed":true}\n'})
        second = profile.current_input(self.args)
        self.assertNotEqual(second, first)
        self.assertEqual(self.generations(), sorted([first, second]))
        # The superseded generation stays intact for concurrent Nix readers.
        self.assertEqual((first / "profile.json").read_bytes(), self.files["profile.json"])

    def test_locally_edited_generation_is_not_reused_or_overwritten(self):
        profile.publish_input(self.args, self.files)
        first = profile.current_input(self.args)
        (first / "profile.json").write_bytes(b"local edit\n")
        with self.assertRaises(profile.ProfileError):
            profile.publish_input(self.args, dict(self.files))
        self.assertEqual(profile.current_input(self.args), first)


if __name__ == "__main__":
    unittest.main()
