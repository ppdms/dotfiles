"""Export broker-created age ciphertext for the configured backup recipient, never plaintext."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

from credential_client import export_record
from credential_validation import FIELDS
from homelab_secrets import atomic_write, private_directory
from principals import manifest


def publish_ciphertext(path, value, backup_uid):
    # Grant backup authority on ciphertext before publication, never on plaintext
    # runtime delivery. The owning group and everyone else retain no file access.
    with tempfile.TemporaryDirectory(prefix=".export-", dir=path.parent) as temporary:
        staged = Path(temporary) / path.name
        atomic_write(staged, value)
        if backup_uid is not None:
            if type(backup_uid) is not int or backup_uid <= 0:
                raise ValueError("Invalid backup principal")
            executable = os.environ.get("HOMELAB_SETFACL", "/run/current-system/sw/bin/setfacl")
            result = subprocess.run(
                [executable, "--set=u::rw-,u:" + str(backup_uid) + ":r--,g::---,m::r--,o::---", "--", str(staged)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=30,
            )
            if result.returncode:
                raise RuntimeError("Ciphertext backup access could not be established")
        staged.replace(path)


def main() -> int:
    export_dir = Path(os.environ["HOMELAB_STATE_ROOT"]) / "secrets-export"
    private_directory(export_dir)
    backrest = manifest()["principals"].get("backrest")
    backup_uid = None if backrest is None else backrest["uid"]
    exported = 0
    failed = False
    for component in sorted(FIELDS):
        try:
            ciphertext = export_record(component)
            if ciphertext is None:
                continue
            if not ciphertext.startswith(b"age-encryption.org/v1\n"):
                raise ValueError("Invalid ciphertext")
            publish_ciphertext(export_dir / (component + ".json.age"), ciphertext, backup_uid)
            exported += 1
        except Exception:
            print("export failed for " + component + ": encrypted export unavailable; prior ciphertext retained", file=sys.stderr)
            failed = True
    print("Exported " + str(exported) + " encrypted credential components")
    return 0 if exported and not failed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Encrypted export failed; previous ciphertext retained", file=sys.stderr)
        raise SystemExit(1)
