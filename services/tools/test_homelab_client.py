import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from credential_layout import encode_payload


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


client = load_module("homelab_client_test_client", "homelab_client.py")


class HomelabClientTests(unittest.TestCase):
    def test_client_package_never_imports_validation(self):
        # Run in a subprocess: other test modules legitimately import
        # credential_validation into this process.
        code = (
            "import sys; sys.path.insert(0, {path!r}); import homelab_client; "
            "assert 'credential_validation' not in sys.modules, 'client imported validation'; "
            "print('isolated')"
        ).format(path=str(Path(__file__).parent))
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("isolated", result.stdout)

    def test_prepare_delivers_files_with_private_modes(self):
        encoded = encode_payload("termix", {"oidc-client-secret": "fixture-secret"})
        with tempfile.TemporaryDirectory() as runtime, patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}), \
                patch.object(client, "read_record", return_value=(encoded, hashlib.sha256(encoded).hexdigest())) as read:
            client.prepare("termix")
            read.assert_called_with("termix", complete=True)
            path = Path(runtime) / "homelab-secrets" / "termix" / "oidc-client-secret"
            self.assertEqual(path.read_text(), "fixture-secret")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_prepare_blocks_on_missing_record(self):
        with tempfile.TemporaryDirectory() as runtime, patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}), \
                patch.object(client, "read_record", return_value=(None, None)):
            with self.assertRaises(RuntimeError):
                client.prepare("termix")


if __name__ == "__main__":
    unittest.main()
