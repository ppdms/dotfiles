import base64
from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from credential_validation import parse_payload, payload_bytes, validate_fields


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


secrets = load_module("authelia_oidc_test_secrets", "homelab-secrets.py")
import homelab_client as client_module

CLIENTS = ("forgejo", "immich", "jellyfin", "termix", "kavita", "readeck")


def fixture_verifier(client):
    salt = ("fixture-salt-" + client).encode()
    digest = hashlib.pbkdf2_hmac("sha512", b"fixture-only-client-password", salt, 1000)
    encode = lambda value: base64.b64encode(value).decode().rstrip("=").replace("+", ".")
    return "$pbkdf2-sha512$1000$" + encode(salt) + "$" + encode(digest)


class AutheliaOIDCCredentialTests(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        self.fields = {client: fixture_verifier(client) for client in CLIENTS}

    def test_every_client_is_required_and_authelia_credential_contracts_are_separate(self):
        for client in CLIENTS:
            fields = {name: value for name, value in self.fields.items() if name != client}
            with self.subTest(client=client):
                with self.assertRaises(ValueError) as error:
                    parse_payload("authelia-oidc", payload_bytes("authelia-oidc", fields))
                self.assertIn(client, str(error.exception))
        with self.assertRaises(ValueError):
            validate_fields("authelia", self.fields, complete=False)
        with self.assertRaises(ValueError):
            validate_fields("authelia-oidc", {"invented-client": fixture_verifier("invented-client")}, complete=False)

    def test_plaintext_and_malformed_hash_diagnostics_never_echo_values(self):
        invalid = (
            "private-plaintext-client-secret",
            "$pbkdf2-sha512$0$Zml4dHVyZQ$PRIVATE_INVALID_HASH",
            "$pbkdf2-sha512$1000$not a salt$PRIVATE_INVALID_HASH",
            "$argon2id$v=19$m=invalid,t=1,p=1$Zml4dHVyZQ$PRIVATE_INVALID_HASH",
        )
        for client in CLIENTS:
            for value in invalid:
                fields = {**self.fields, client: value}
                with self.subTest(client=client, value_kind=value.split("$")[0]):
                    with self.assertRaises(ValueError) as error:
                        parse_payload("authelia-oidc", payload_bytes("authelia-oidc", fields))
                    self.assertIn(client, str(error.exception))
                    self.assertNotIn(value, str(error.exception))
                    self.assertNotIn("PRIVATE_INVALID_HASH", str(error.exception))

    def test_runtime_verifier_files_are_private(self):
        encoded = payload_bytes("authelia-oidc", self.fields)
        with tempfile.TemporaryDirectory() as runtime, patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}), \
                patch.object(client_module, "read_record", return_value=(encoded, hashlib.sha256(encoded).hexdigest())), \
                patch.object(sys, "argv", ["homelab-secrets", "prepare", "authelia-oidc"]):
            secrets.main()
            directory = Path(runtime) / "homelab-secrets" / "authelia-oidc"
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for client in CLIENTS:
                path = directory / client
                self.assertTrue(stat.S_ISREG(path.lstat().st_mode))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse((Path(runtime) / "homelab-secrets" / "authelia").exists())

    def test_absent_runtime_bundle_blocks_with_named_missing_fields(self):
        diagnostics = io.StringIO()
        with tempfile.TemporaryDirectory() as runtime, patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}), \
                patch.object(client_module, "read_record", return_value=(None, None)), \
                patch.object(sys, "argv", ["homelab-secrets", "prepare", "authelia-oidc"]), \
                redirect_stderr(diagnostics), self.assertRaises(RuntimeError):
            secrets.main()
        for client in CLIENTS:
            self.assertIn(client, diagnostics.getvalue())

    def test_invalid_import_cannot_write_encrypted_record(self):
        value = "private-plaintext-client-secret"
        with tempfile.TemporaryDirectory() as runtime, patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}), \
                patch.object(secrets, "read_record", return_value=(None, None)), \
                patch.object(secrets, "store_record") as store:
            private_input = Path(runtime) / "input"
            private_input.write_text(value)
            private_input.chmod(0o600)
            with patch.object(sys, "argv", ["homelab-secrets", "import", "authelia-oidc", "forgejo", str(private_input)]):
                with self.assertRaises(ValueError) as error:
                    secrets.main()
            self.assertNotIn(value, str(error.exception))
            store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
