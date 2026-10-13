"""Hard-link boundaries for reader grants and independent state copies."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("service_state", Path(__file__).with_name("service-state.py"))
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class HardLinkScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.outside = self.root / "outside"
        self.outside.write_bytes(b"original dependency bytes\n")
        self.outside.chmod(0o666)
        self.link = self.source / "dependency"
        os.link(self.outside, self.link)
        native = os.environ.get("HOMELAB_SETFACL") or shutil.which("setfacl")
        copier = os.environ.get("HOMELAB_CP") or shutil.which("cp")
        if not native or not copier:
            self.skipTest("native ACL and copy tools required")
        self.environment = patch.dict(os.environ, {"HOMELAB_SETFACL": native, "HOMELAB_CP": copier})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_reader_leaves_public_external_hardlink_metadata_unchanged(self):
        before = self.outside.stat()
        state.change_tree(self.source, reader=43006)
        after = self.outside.stat()
        self.assertEqual((after.st_ino, after.st_uid, after.st_gid, after.st_mode, after.st_ctime_ns),
                         (before.st_ino, before.st_uid, before.st_gid, before.st_mode, before.st_ctime_ns))
        self.assertEqual(self.outside.read_bytes(), b"original dependency bytes\n")

    def test_reader_and_owner_reject_private_external_hardlink(self):
        self.outside.chmod(0o600)
        before = self.outside.stat()
        for operation in ({"reader": 43006}, {"owner": (os.getuid(), os.getgid())}):
            with self.subTest(operation=operation), self.assertRaises(state.StateError):
                state.change_tree(self.source, **operation)
        self.assertEqual(self.outside.stat().st_ctime_ns, before.st_ctime_ns)

    def test_named_acl_cannot_be_bypassed_by_public_mode_bits(self):
        subprocess.run([state.tool("setfacl"), "-m", "u:43006:---", str(self.outside)], check=True)
        self.assertEqual(self.outside.stat().st_mode & 0o444, 0o444)
        with self.assertRaises(state.StateError):
            state.change_tree(self.source, reader=43006)

    def test_copy_owns_new_inode_without_mutating_shared_source(self):
        self.outside.chmod(0o600)
        before = self.outside.stat()
        target = self.root / "target"
        target.mkdir()
        state.copy_empty(self.source, target, {"uid": os.getuid(), "gid": os.getgid()})
        copied = target / "dependency"
        self.assertEqual(copied.read_bytes(), self.outside.read_bytes())
        self.assertNotEqual(copied.stat().st_ino, self.outside.stat().st_ino)
        self.assertEqual((copied.stat().st_uid, copied.stat().st_gid), (os.getuid(), os.getgid()))
        after = self.outside.stat()
        self.assertEqual((after.st_ino, after.st_nlink, after.st_mode, after.st_ctime_ns),
                         (before.st_ino, before.st_nlink, before.st_mode, before.st_ctime_ns))


if __name__ == "__main__":
    unittest.main()
