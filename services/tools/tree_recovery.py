"""Cold, root-controlled checkpoints for the Tree release lifecycle.

The caller holds ``lock()`` across the operation and stops every writer before
checkpoint/restore. No method starts services or makes an image decision.
"""

from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid


class RecoveryError(RuntimeError):
    pass


_ID = re.compile(r"[a-f0-9]{32}\Z")
_PHASES = {"begun", "ready", "restoring", "restored"}
_DATASET = ("jobs", "objects", "session", "code")
_FICLONE = 0x40049409
_COPY_FALLBACK = {errno.EXDEV, errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL, errno.ENOSYS}


def _path(value: Path | str) -> Path:
    text = str(value)
    result = Path(text)
    if (
        not result.is_absolute()
        or result == Path("/")
        or ".." in result.parts
        or str(result) != text
        or any(character in text for character in "\x00\n\r")
    ):
        raise RecoveryError("recovery paths must be canonical absolute non-root paths")
    return result


@contextmanager
def _directory(path: Path):
    """Open every component without following symlinks, including ancestors."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                child = os.open(component, flags | getattr(os, "O_NOATIME", 0), dir_fd=descriptor)
            except PermissionError:
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _sync(path: Path) -> None:
    with _directory(path) as descriptor:
        os.fsync(descriptor)


def _metadata(path: Path):
    with _directory(path.parent) as descriptor:
        return os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)


def _exists(path: Path) -> bool:
    try:
        _metadata(path)
        return True
    except FileNotFoundError:
        return False


def _copy_metadata(source: Path, destination: Path, info) -> None:
    # chown may clear set-id bits; apply the exact mode and ACLs afterwards.
    os.chown(destination, info.st_uid, info.st_gid, follow_symlinks=False)
    shutil.copystat(source, destination, follow_symlinks=False)
    os.chmod(destination, stat.S_IMODE(info.st_mode), follow_symlinks=False)
    os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)


def _copy_file(source: Path, destination: Path, info, *, immutable: bool = False) -> None:
    if immutable:
        try:
            os.link(source, destination, follow_symlinks=False)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
        else:
            linked = _metadata(destination)
            if not stat.S_ISREG(linked.st_mode) or (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino):
                raise RecoveryError(f"Tree object changed while linking: {source}")
            # Object publishers replace paths atomically; they never mutate an
            # existing object inode. A link already preserves exact metadata.
            # In particular, chown/chmod here would modify the live/shared inode.
            return
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        original = os.open(source, flags | getattr(os, "O_NOATIME", 0))
    except PermissionError:
        original = os.open(source, flags)
    try:
        pinned = os.fstat(original)
        if not stat.S_ISREG(pinned.st_mode) or (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino):
            raise RecoveryError(f"Tree state changed while copying: {source}")
        output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            try:
                # Mutable state always needs independent inodes; cross-device
                # immutable objects also fall back to reflinks or ordinary copies.
                fcntl.ioctl(output, _FICLONE, original)
            except OSError as error:
                if error.errno not in _COPY_FALLBACK:
                    raise
                try:
                    while os.copy_file_range(original, output, 1024 * 1024):
                        pass
                except OSError as copy_error:
                    if copy_error.errno not in _COPY_FALLBACK:
                        raise
                    while chunk := os.read(original, 1024 * 1024):
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(output, view) :]
            _copy_metadata(source, destination, info)
            os.fsync(output)
        finally:
            os.close(output)
    finally:
        os.close(original)


def _copy_tree(source: Path, destination: Path, *, immutable: bool = False) -> None:
    info = _metadata(source)
    if not stat.S_ISDIR(info.st_mode):
        raise RecoveryError(f"Tree state must be a real directory, not a symlink: {source}")
    destination.mkdir(mode=0o700)
    with _directory(source) as descriptor:
        pinned = os.fstat(descriptor)
        if (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino):
            raise RecoveryError(f"Tree state changed while copying: {source}")
        for name in os.listdir(descriptor):
            child = source / name
            child_info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(child_info.st_mode):
                _copy_tree(child, destination / name, immutable=immutable)
            elif stat.S_ISREG(child_info.st_mode):
                _copy_file(child, destination / name, child_info, immutable=immutable)
            else:
                raise RecoveryError(f"Tree checkpoint refuses symlinks and special files: {child}")
    _copy_metadata(source, destination, info)
    _sync(destination)


def _validate_tree(path: Path) -> None:
    info = _metadata(path)
    if not stat.S_ISDIR(info.st_mode):
        raise RecoveryError(f"Tree state must be a real directory, not a symlink: {path}")
    with _directory(path) as descriptor:
        for name in os.listdir(descriptor):
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _validate_tree(path / name)
            elif not stat.S_ISREG(info.st_mode):
                raise RecoveryError(f"Tree recovery refuses symlinks and special files: {path / name}")


def _copy_component(source: Path, destination: Path) -> None:
    info = _metadata(source)
    if stat.S_ISDIR(info.st_mode):
        _copy_tree(source, destination, immutable=source.name == "objects")
    elif stat.S_ISREG(info.st_mode):
        _copy_file(source, destination, info)
    else:
        raise RecoveryError(f"Tree recovery refuses symlinks and special files: {source}")


def _copy_dataset(source: Path, destination: Path) -> None:
    # Manual .predeploy-* checkpoints and unrelated root siblings are not Tree's
    # managed dataset. Never recurse into or remove those operator-owned paths.
    with _directory(source):
        pass
    destination.mkdir(mode=0o700)
    for name in _DATASET:
        if _exists(source / name):
            _copy_component(source / name, destination / name)
    _sync(destination)


def _validate_dataset(path: Path) -> None:
    with _directory(path):
        pass
    for name in _DATASET:
        info = _metadata(path / name)
        expected = stat.S_ISDIR if name in ("jobs", "objects") else stat.S_ISREG
        if not expected(info.st_mode):
            raise RecoveryError(f"required Tree state must not be a symlink or special file: {path / name}")


class RecoveryStore:
    def __init__(self, directory: Path):
        self.directory = _path(directory)
        self._locked = False

    def _root(self, *, create: bool) -> bool:
        # Parents may be root-owned or caller-owned, but never service-writable.
        # A root-owned sticky /tmp is safe for disposable unittest directories.
        with _directory(self.directory.parent):
            for ancestor in reversed(self.directory.parents):
                info = ancestor.stat()
                sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                if info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and not sticky_root):
                    raise RecoveryError(f"unsafe recovery parent: {ancestor}")
            if create:
                try:
                    self.directory.mkdir(mode=0o755)
                    # tree-dev deliberately uses umask 077; workload users must
                    # nevertheless be able to stat the root-owned startup block.
                    with _directory(self.directory) as descriptor:
                        os.fchmod(descriptor, 0o755)
                        os.fsync(descriptor)
                    _sync(self.directory.parent)
                except FileExistsError:
                    pass
            try:
                info = _metadata(self.directory)
            except FileNotFoundError:
                return False
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o755:
                raise RecoveryError(f"unsafe recovery root (ownership, permissions or symlink): {self.directory}")
        return True

    def _private_directory(self, path: Path, *, create: bool = False) -> None:
        if create:
            try:
                path.mkdir(mode=0o700)
                _sync(path.parent)
            except FileExistsError:
                pass
        info = _metadata(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RecoveryError(f"unsafe private recovery directory: {path}")

    def _read_json(self, path: Path) -> dict | None:
        with _directory(path.parent) as parent:
            try:
                descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                return None
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                ):
                    raise RecoveryError(f"unsafe private recovery record: {path}")
                content = stream.read(65537)
                if len(content) > 65536:
                    raise RecoveryError(f"oversized recovery record: {path}")
                try:
                    value = json.loads(content)
                except (ValueError, UnicodeError) as error:
                    raise RecoveryError(f"malformed recovery record: {path}") from error
                if not isinstance(value, dict):
                    raise RecoveryError(f"malformed recovery record: {path}")
                return value

    def _write_json(self, path: Path, value: dict) -> None:
        if _exists(path):
            self._read_json(path)
        descriptor, name = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _sync(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _operation(self, record: dict, *, create: bool = False) -> Path:
        parent = self.directory / "checkpoints"
        self._private_directory(parent, create=create)
        operation = parent / record["id"]
        self._private_directory(operation, create=create)
        return operation

    def _validate_record(self, record: dict) -> dict:
        required = {"id", "state", "image", "candidate", "was_running", "checkpoint", "phase", "admitted", "rescue"}
        if set(record) not in (required, required | {"outcome"}):
            raise RecoveryError("malformed pending recovery fields")
        if (
            not isinstance(record["id"], str)
            or not _ID.fullmatch(record["id"])
            or not isinstance(record["phase"], str)
            or record["phase"] not in _PHASES
            or type(record["was_running"]) is not bool
            or type(record["admitted"]) is not bool
        ):
            raise RecoveryError("malformed pending recovery state")
        for key in ("image", "candidate"):
            value = record[key]
            if (
                not isinstance(value, str)
                or not value
                or any(character.isspace() or ord(character) < 32 for character in value)
            ):
                raise RecoveryError(f"invalid recovery {key}")
        if not isinstance(record["state"], str):
            raise RecoveryError("invalid recovery state path")
        state = _path(record["state"])
        if state == self.directory or state in self.directory.parents or self.directory in state.parents:
            raise RecoveryError("Tree state and recovery storage must not overlap")
        operation = self.directory / "checkpoints" / record["id"]
        if record["checkpoint"] != str(operation / "snapshot"):
            raise RecoveryError("invalid checkpoint path in recovery record")
        if record["rescue"] not in (None, str(operation / "rescue")):
            raise RecoveryError("invalid rescue path in recovery record")
        if (record["phase"] in {"restoring", "restored"}) != (record["rescue"] is not None):
            raise RecoveryError("inconsistent recovery rescue state")
        if "outcome" in record and (
            not isinstance(record["outcome"], str) or record["outcome"] not in {"activated", "restored"}
        ):
            raise RecoveryError("invalid completed recovery outcome")
        return record

    @contextmanager
    def lock(self):
        try:
            self._root(create=True)
            descriptor = os.open(self.directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError as error:
            raise RecoveryError(f"Tree recovery lock unavailable: {error}") from error
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise RecoveryError("unsafe Tree recovery lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RecoveryError("another Tree lifecycle operation holds the recovery lock") from None
            self._locked = True
            try:
                yield self
            finally:
                self._locked = False
        finally:
            os.close(descriptor)

    def _require_lock(self) -> None:
        if not self._locked:
            raise RecoveryError("Tree recovery mutations require this store's operation lock")
        self._root(create=False)

    def pending(self) -> dict | None:
        try:
            if not self._root(create=False):
                return None
            pending = self._read_json(self.directory / "pending.json")
            blocked = self._read_json(self.directory / "blocked")
            # blocked is also the write-ahead begin intent: a crash before the
            # pending publication remains explicitly recoverable and fail-closed.
            if pending is None:
                return self._validate_record(blocked) if blocked is not None else None
            self._validate_record(pending)
            if blocked is not None:
                self._validate_record(blocked)
                if blocked["id"] != pending["id"]:
                    raise RecoveryError("startup block belongs to a different pending operation")
            elif not pending["admitted"] and "outcome" not in pending:
                raise RecoveryError("pending Tree recovery is missing its startup block")
            return pending
        except OSError as error:
            raise RecoveryError(f"cannot read trusted Tree recovery state: {error}") from error

    def _current(self, record: dict) -> dict:
        self._require_lock()
        self._validate_record(record)
        current = self.pending()
        immutable = ("id", "state", "image", "candidate", "was_running", "checkpoint")
        if current is None or any(current[key] != record[key] for key in immutable):
            raise RecoveryError("record does not match the pending Tree recovery operation")
        record.clear()
        record.update(current)
        return record

    def _publish(self, record: dict) -> None:
        self._write_json(self.directory / "pending.json", record)

    def _revoke_migration(self) -> None:
        self._require_lock()
        (self.directory / "migration.env").unlink(missing_ok=True)
        _sync(self.directory)

    def begin(self, state: Path, image: str, candidate: str, was_running: bool) -> dict:
        self._require_lock()
        if self.pending() is not None:
            raise RecoveryError("an unresolved Tree release exists; run tree-dev recover")
        state = _path(state)
        _validate_dataset(state)
        identifier = uuid.uuid4().hex
        record = {
            "id": identifier,
            "state": str(state),
            "image": image,
            "candidate": candidate,
            "was_running": was_running,
            "checkpoint": str(self.directory / "checkpoints" / identifier / "snapshot"),
            "phase": "begun",
            "admitted": False,
            "rescue": None,
        }
        self._validate_record(record)
        # Publish the complete blocked intent FIRST. If pending publication fails,
        # pending() can recover it and no caller can reach migration.
        self._write_json(self.directory / "blocked", record)
        self._revoke_migration()
        self._publish(record)
        return record

    def checkpoint(self, record: dict) -> Path:
        record = self._current(record)
        if record["phase"] != "begun":
            raise RecoveryError("Tree checkpoint is already ready or recovery has begun")
        operation = self._operation(record, create=True)
        snapshot = Path(record["checkpoint"])
        staging = operation / ".snapshot-incomplete"
        # Only our own deterministic incomplete staging is ever removed. An
        # existing completed snapshot after a crash can be published as ready.
        if not _exists(snapshot):
            if _exists(staging):
                _validate_tree(staging)
                shutil.rmtree(staging)
            _validate_dataset(Path(record["state"]))
            _copy_dataset(Path(record["state"]), staging)
            os.rename(staging, snapshot)
            _sync(operation)
        else:
            self._private_directory(snapshot)
            _validate_dataset(snapshot)
            _validate_tree(snapshot)
        updated = dict(record, phase="ready")
        self._publish(updated)
        record.update(updated)
        return snapshot

    def allow_migration(self, record: dict) -> None:
        record = self._current(record)
        if record["phase"] != "ready" or record["admitted"] or "outcome" in record:
            raise RecoveryError("migration requires a completed, unadmitted release checkpoint")
        self._private_directory(Path(record["checkpoint"]))
        _validate_dataset(Path(record["checkpoint"]))
        digest = record["candidate"].removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RecoveryError("migration candidate must be an immutable image identity")
        descriptor, name = tempfile.mkstemp(prefix=".migration-", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(f"TREE_RELEASE_IMAGE=sha256:{digest}\n")
                stream.flush()
                # Only the immutable image ID is public; never keys or state.
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / "migration.env")
            _sync(self.directory)
        finally:
            temporary.unlink(missing_ok=True)

    def block(self, record: dict) -> None:
        record = self._current(record)
        # Revoke BEFORE stopping the manager-owned launcher. A delayed StartUnit
        # request then fails its mandatory EnvironmentFile before spawning.
        self._revoke_migration()
        updated = dict(record, admitted=False)
        self._write_json(self.directory / "blocked", updated)
        self._publish(updated)
        record.update(updated)

    def restore(self, record: dict) -> Path | None:
        record = self._current(record)
        self.block(record)
        if record["phase"] == "begun":
            return None
        rescue = Path(record["rescue"]) if record["rescue"] else None
        if record["phase"] == "restored":
            self._operation(record)
            self._private_directory(rescue)
            _validate_tree(rescue)
            return rescue
        operation = self._operation(record)
        snapshot = Path(record["checkpoint"])
        self._private_directory(snapshot)
        _validate_dataset(snapshot)
        _validate_tree(snapshot)
        state = Path(record["state"])
        with _directory(state):
            pass
        rescue = operation / "rescue"
        if record["phase"] == "ready":
            if not _exists(rescue):
                staging = operation / ".rescue-incomplete"
                if _exists(staging):
                    _validate_tree(staging)
                    shutil.rmtree(staging)
                _copy_dataset(state, staging)
                os.rename(staging, rescue)
                _sync(operation)
            else:
                self._private_directory(rescue)
                _validate_tree(rescue)
            updated = dict(record, phase="restoring", rescue=str(rescue))
            self._publish(updated)
            record.update(updated)
        else:
            self._private_directory(rescue)
            _validate_tree(rescue)
        # The first failed dataset is durable before any live deletion. Replay
        # replaces only the four managed components, never unrelated root
        # siblings or the first rescue.
        for name in _DATASET:
            source, target = snapshot / name, state / name
            if _exists(target):
                info = _metadata(target)
                if stat.S_ISDIR(info.st_mode):
                    _validate_tree(target)
                    shutil.rmtree(target)
                elif stat.S_ISREG(info.st_mode):
                    target.unlink()
                else:
                    raise RecoveryError(f"Tree recovery refuses symlinks and special files: {target}")
                _sync(state)
            _copy_component(source, target)
            _sync(state)
        updated = dict(record, phase="restored")
        self._publish(updated)
        record.update(updated)
        return rescue

    def admit(self, record: dict) -> None:
        record = self._current(record)
        if record["phase"] == "restoring":
            raise RecoveryError("cannot admit an incomplete Tree restoration")
        self._revoke_migration()
        updated = dict(record, admitted=True)
        # Journal admission first: interruption before unlink merely stays blocked.
        self._publish(updated)
        record.update(updated)
        (self.directory / "blocked").unlink(missing_ok=True)
        _sync(self.directory)

    def complete(self, record: dict, outcome: str) -> None:
        record = self._current(record)
        if outcome not in {"activated", "restored"}:
            raise RecoveryError("Tree recovery outcome must be activated or restored")
        if not record["admitted"]:
            raise RecoveryError("Tree recovery must be admitted before completion")
        if outcome == "activated" and record["phase"] != "ready":
            raise RecoveryError("cannot activate without a complete Tree checkpoint")
        if outcome == "restored" and record["phase"] not in {"begun", "restored"}:
            raise RecoveryError("cannot complete an unfinished Tree restoration")
        operation = self._operation(record, create=True)
        updated = dict(record, outcome=outcome)
        self._write_json(operation / "record.json", updated)
        self._publish(updated)
        record.update(updated)
        # History is durable first. Clear block before pending, so a crash never
        # strands an orphan block after the last recoverable journal is removed.
        (self.directory / "blocked").unlink(missing_ok=True)
        _sync(self.directory)
        (self.directory / "pending.json").unlink()
        _sync(self.directory)
