"""Ciphertext exclusion applies to every tracked path, including examples."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PublicationPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.private = Path(temporary.name)
        self.checkout = self.private / "checkout"
        (self.checkout / "tools").mkdir(parents=True)
        shutil.copyfile(ROOT / "tools/check-secrets.sh",
                        self.checkout / "tools/check-secrets.sh")
        self.git("init", "--initial-branch=main")
        self.git("add", "tools/check-secrets.sh")
        self.git("commit", "-m", "init")

    def git(self, *arguments):
        return subprocess.run([
            "git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
            "-c", "user.name=Publication Fixture", "-c", "user.email=fixture@example.test",
            "-C", str(self.checkout), *arguments,
        ], capture_output=True, check=True)

    def stage(self, relative, content):
        destination = self.checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        self.git("add", "--", relative)

    def check(self):
        return subprocess.run(["bash", "tools/check-secrets.sh"],
                              cwd=self.checkout, capture_output=True, text=True)

    def assert_rejected(self, relative):
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(relative, result.stdout)
        return result

    def age_recipient(self):
        if not shutil.which("age") or not shutil.which("age-keygen"):
            self.skipTest("age tools required")
        identity = self.private / "identity.txt"
        subprocess.run(["age-keygen", "-o", str(identity)], capture_output=True, check=True)
        return subprocess.run(["age-keygen", "-y", str(identity)],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_plain_settings_and_public_certificates_are_allowed(self):
        self.stage("examples/macbook/profile.json", b'{"example": true}\n')
        self.stage("examples/server/trust/lan-root-ca.pem",
                   (ROOT / "examples/server/trust/lan-root-ca.pem").read_bytes())
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_opaque_credential_filename_is_rejected(self):
        path = "examples/server/profile.cred"
        self.stage(path, b"\x00opaque credential payload")
        self.assert_rejected(path)

    def test_private_key_is_rejected_without_disclosure(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl required")
        payload = subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519"],
                                 capture_output=True, check=True).stdout
        path = "private-key.pem"
        self.stage(path, payload)
        result = self.assert_rejected(path)
        self.assertNotIn(payload.decode().splitlines()[1], result.stdout + result.stderr)

    def test_tokens_are_rejected_without_disclosure(self):
        tokens = (
            ("github-token.txt", "gh" + "p_" + "a" * 36),
            ("api-token.txt", "sk-" + "b" * 36),
            ("age-identity.txt", "AGE-SECRET-KEY-" + "1" + "A" * 58),
        )
        for path, token in tokens:
            with self.subTest(path=path):
                self.stage(path, token.encode())
                result = self.assert_rejected(path)
                self.assertNotIn(token, result.stdout + result.stderr)

    def test_personal_home_path_in_examples_is_rejected_without_disclosure(self):
        path = "examples/macbook/private-settings.json"
        home = "/Users/" + "privateaccount/"
        self.stage(path, ('{"home": "' + home + '"}\n').encode())
        result = self.assert_rejected(path)
        self.assertNotIn(home, result.stdout + result.stderr)

    def test_untracked_files_fail_without_disclosure(self):
        path = "pending-secret.txt"
        token = "gh" + "p_" + "c" * 36
        (self.checkout / path).write_text(token)
        result = self.assert_rejected(path)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_raw_and_armored_age_payloads_without_age_suffix_are_rejected(self):
        recipient = self.age_recipient()
        for armored in (False, True):
            with self.subTest(armored=armored):
                command = ["age", "--encrypt", "--recipient", recipient]
                if armored:
                    command.append("--armor")
                payload = subprocess.run(command, input=b"publication-policy-fixture",
                                         capture_output=True, check=True).stdout
                path = "examples/macbook/payload.txt"
                self.stage(path, payload)
                result = self.assert_rejected(path)
                self.assertNotIn(payload.decode("ascii", errors="ignore"), result.stdout)

    def test_sops_payload_in_ordinary_yaml_is_rejected(self):
        if not shutil.which("sops"):
            self.skipTest("sops required")
        recipient = self.age_recipient()
        payload = subprocess.run([
            "sops", "--encrypt", "--age", recipient, "--input-type", "json",
            "--output-type", "yaml", "/dev/stdin",
        ], cwd=self.private, input=b'{"notice":"publication-policy-fixture"}',
            capture_output=True, check=True).stdout
        path = "examples/macbook/settings.yaml"
        self.stage(path, payload)
        self.assert_rejected(path)


if __name__ == "__main__":
    unittest.main()
