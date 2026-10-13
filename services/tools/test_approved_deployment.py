#!/usr/bin/env python3
"""Root-only boundary regression using an actually built approval artifact.

Run as root with APPROVED_DEPLOYMENT_ARTIFACT and APPROVED_DEPLOYMENT_RUNTIME
pointing to native Nix build outputs. No system activation or vendor fetch runs.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ApprovalBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise unittest.SkipTest("Run the native approval fixture as root")
        cls.artifact = Path(os.environ["APPROVED_DEPLOYMENT_ARTIFACT"])
        cls.runtime = Path(os.environ["APPROVED_DEPLOYMENT_RUNTIME"]) / "bin/homelab-approved-deployment"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="approved-deployment-", dir="/var/lib"))
        self.directory = self.root / "deployment"
        self.gc_root = self.root / "gc-root"

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_helper(self, *args, success=True):
        result = subprocess.run([
            str(self.runtime), "--directory", str(self.directory),
            "--gc-root", str(self.gc_root), *map(str, args),
        ], capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, "Unsafe approval was accepted")
        return result

    def approve(self):
        self.run_helper("install", self.artifact)

    def test_reapproval_retains_private_root_ownership_and_permissions(self):
        self.approve()
        self.approve()
        self.assertEqual(self.directory.stat().st_uid, 0)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        for name in ("flake.nix", "flake.lock"):
            installed = self.directory / name
            self.assertEqual(installed.stat().st_uid, 0)
            self.assertEqual(installed.stat().st_mode & 0o777, 0o600)

    def test_missing_approval_blocks_update(self):
        self.run_helper("update", success=False)

    def test_user_owned_lock_blocks_update_and_install(self):
        self.approve()
        os.chown(self.directory / "flake.lock", 65534, 65534)
        self.run_helper("update", success=False)
        self.run_helper("install", self.artifact, success=False)

    def test_writable_approval_blocks_update(self):
        self.approve()
        self.directory.chmod(0o770)
        self.run_helper("update", success=False)

    def test_lock_symlink_blocks_update_and_install(self):
        self.approve()
        lock = self.directory / "flake.lock"
        lock.unlink()
        lock.symlink_to(self.artifact / "flake.lock")
        self.run_helper("update", success=False)
        self.run_helper("install", self.artifact, success=False)

    def test_parent_symlink_is_not_traversed(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        self.directory = link / "deployment"
        self.run_helper("install", self.artifact, success=False)
        self.assertEqual(list(real.iterdir()), [])

    def test_user_path_cannot_supply_an_artifact(self):
        self.run_helper("install", self.root, success=False)
        self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
