"""Quadlet-side credential delivery client (prepare/persist only).

This module must never import credential_validation: its package's store
path is embedded in every Quadlet, so validation edits must not restart
containers. The broker performs complete validation when the client
requests a record with complete=True.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile

from credential_client import read_record, update_mutable
from credential_layout import FIELDS, MUTABLE, OPTIONAL, decode_payload, required_fields


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or path.resolve() != path:
        raise RuntimeError("Runtime directory is not a user-owned directory")
    path.chmod(0o700)


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise RuntimeError("Secret input must be a private user-owned regular file")
        return stream.read()


def atomic_write(path, value):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def component_lock(component, nonblocking=False):
    if component not in FIELDS:
        raise RuntimeError("Unknown credential component")
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    info = runtime.lstat()
    if (not runtime.is_absolute() or runtime.resolve() != runtime or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("XDG_RUNTIME_DIR must be a private user-owned directory")
    private_directory(runtime / "homelab-secrets")
    directory = runtime / "homelab-secrets" / component
    private_directory(directory)
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        info = os.fstat(lock.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise RuntimeError("Credential lock is not a private user-owned file")
        operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(lock, operation)
        except BlockingIOError:
            raise RuntimeError("Another credential operation is active for this component") from None
        yield directory


def load_record(component, complete=False):
    value, digest = read_record(component, complete=complete)
    return ({} if value is None else decode_payload(component, value), digest)


def load_fields(component, complete=False):
    return load_record(component, complete=complete)[0]


def load_field(component, field):
    if component not in FIELDS or field not in FIELDS[component]:
        raise RuntimeError("Unknown credential field")
    value = load_fields(component).get(field)
    return None if value is None else value.encode("utf-8")


def runtime_digest(component, directory):
    field = MUTABLE.get(component)
    if field is None:
        return None
    path = directory / FIELDS[component][field]
    return hashlib.sha256(private_read(path)).hexdigest().encode() if path.exists() else None


def persist(component, directory):
    field = MUTABLE.get(component)
    if field is None:
        return
    path = directory / FIELDS[component][field]
    baseline = directory / (field + ".sha256")
    if not path.exists() or not baseline.exists():
        return
    value = private_read(path)
    digest = hashlib.sha256(value).hexdigest().encode()
    previous_digest = private_read(baseline)
    if digest == previous_digest:
        return
    # Never send immutable fields from a service. The broker merges this one
    # catalogued field under its own lock after checking the original baseline.
    update_mutable(component, field, value.decode("utf-8"), previous_digest.decode("ascii"))
    atomic_write(baseline, digest)


def persist_locked(component):
    with component_lock(component) as directory:
        persist(component, directory)


def prepare(component, allow_missing=False):
    with component_lock(component) as directory:
        persist(component, directory)
        # complete=True makes the broker run full content validation
        # (credential_validation) before any file is delivered.
        fields = load_fields(component, complete=True)
        missing = sorted(set(required_fields(component)) - set(fields))
        if missing:
            print("Missing encrypted inputs for " + component + ": " + ", ".join(missing), file=sys.stderr)
            if not allow_missing:
                raise RuntimeError("Required encrypted credential inputs must be available before starting this component")
        optional = OPTIONAL.get(component, ())
        for field, relative in FIELDS[component].items():
            path = directory / relative
            private_directory(path.parent)
            if field in fields and (fields[field] or field not in optional):
                value = fields[field].encode("utf-8")
                atomic_write(path, value)
                if field == MUTABLE.get(component):
                    atomic_write(directory / (field + ".sha256"), hashlib.sha256(value).hexdigest().encode())
            elif field in optional:
                # Unset: drop what an earlier start delivered, so the consumer
                # never keeps running on a withdrawn value.
                path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Deliver scoped broker credentials to private container runtime files")
    parser.add_argument("operation", choices=("prepare", "persist"))
    parser.add_argument("component", choices=tuple(FIELDS))
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.operation == "prepare":
        prepare(args.component, allow_missing=args.allow_missing)
    else:
        persist_locked(args.component)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Credential operation interrupted; check stored credentials before retrying.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        # Input parser, native backend and socket failures never reach diagnostics.
        print("Secret operation failed; check authorization, input format, broker availability and concurrent edits. Existing runtime files retained.", file=sys.stderr)
        sys.exit(1)
