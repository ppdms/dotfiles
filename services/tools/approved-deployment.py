#!/usr/bin/env python3
"""Install reviewed store artifacts and update only approved stable vendors."""
import argparse
import ctypes
import fcntl
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


class ApprovalError(Exception):
    pass


def trusted_directory(path, *, create=False, private=False):
    """Walk only root-owned directories, never links or writable ancestors."""
    if not path.is_absolute() or ".." in path.parts:
        raise ApprovalError("Approval paths must be absolute without parent traversal")
    current = Path("/")
    info = current.lstat()
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise ApprovalError(f"Missing approved deployment directory: {current}") from None
            current.mkdir(mode=0o700)
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ApprovalError(f"Unsafe approval directory ownership, permissions or link: {current}")
    if private and stat.S_IMODE(info.st_mode) != 0o700:
        raise ApprovalError(f"Approved deployment directory must be root-owned mode 0700: {path}")


def approved_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ApprovalError(f"Approved file must be root-owned regular mode 0600: {path}")


def check_approval(directory):
    trusted_directory(directory, private=True)
    for name in ("flake.nix", "flake.lock"):
        try:
            approved_file(directory / name)
        except FileNotFoundError:
            raise ApprovalError(f"Missing approved {name}; approve a reviewed configuration with a manual switch first") from None


def exchange_directories(left, right):
    # Linux rename exchange keeps the flake and its exact lock one transaction.
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def install(artifact, directory, gc_root):
    # Do not read a user path as root: the candidate was built into the store.
    if not artifact.is_absolute() or artifact.parent != Path("/nix/store"):
        raise ApprovalError("Approval requires an immutable top-level /nix/store artifact")
    trusted_directory(Path("/nix"))
    store = Path("/nix/store").lstat()
    # The multi-user Nix store is group-writable with the sticky bit. Its
    # immutable root-owned entries cannot be replaced by those build users.
    if (not stat.S_ISDIR(store.st_mode) or store.st_uid != 0 or store.st_mode & 0o002
            or (store.st_mode & 0o020 and not store.st_mode & stat.S_ISVTX)):
        raise ApprovalError("Unsafe Nix store ownership, permissions or link")
    info = artifact.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o222:
        raise ApprovalError("Approval artifact is not an immutable root-owned store directory")
    trusted_directory(gc_root.parent, create=True)
    if gc_root.exists() or gc_root.is_symlink():
        info = gc_root.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            raise ApprovalError("Unsafe approved deployment GC root")
    if directory.exists() or directory.is_symlink():
        check_approval(directory)
    staged = Path(tempfile.mkdtemp(prefix=".approved-", dir=directory.parent))
    try:
        for name in ("flake.nix", "flake.lock"):
            source = artifact / name
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o222:
                raise ApprovalError(f"Approval artifact has an unsafe {name}")
            with source.open("rb") as incoming, (staged / name).open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                os.fchmod(outgoing.fileno(), 0o600)
        root_stage = gc_root.parent / ("." + gc_root.name + "." + staged.name)
        os.symlink(artifact, root_stage)
        try:
            os.replace(root_stage, gc_root)
        finally:
            root_stage.unlink(missing_ok=True)
        if directory.exists():
            exchange_directories(staged, directory)
        else:
            os.rename(staged, directory)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def update(directory):
    check_approval(directory)
    subprocess.run([
        os.environ["HOMELAB_NIX"], "--extra-experimental-features", "nix-command flakes",
        "flake", "update", "nixpkgs", "home-manager",
        "--flake", "path:" + str(directory),
    ], check=True)
    # Nix's atomic rewrite inherits this process's private umask.
    check_approval(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/var/lib/nix-config/deployment"))
    parser.add_argument("--gc-root", type=Path, default=Path("/nix/var/nix/gcroots/nix-config-approved"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("update")
    approval = commands.add_parser("install")
    approval.add_argument("artifact", type=Path)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ApprovalError("Configuration approval and vendor updates require root")
        os.umask(0o077)
        trusted_directory(args.directory.parent, create=args.command == "install", private=True)
        lock = args.directory.parent / ".approval.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            approved_file(lock)
            fcntl.flock(fd, fcntl.LOCK_EX)
            if args.command == "install":
                install(args.artifact, args.directory, args.gc_root)
            else:
                update(args.directory)
        finally:
            os.close(fd)
        return 0
    except (ApprovalError, OSError, subprocess.CalledProcessError) as error:
        print(f"approved-deployment: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
