#!/usr/bin/env python3
"""Root-only state preparation and private bind views for rootless roles.

No application identity is generated here. Sources are immutable manifest metadata;
only explicitly declared workload units are stopped, and nonempty target data wins.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile

from credential_client import CredentialError, export_record, read_record
from credential_broker import BrokerError, NativeCrypto, private_read
from credential_validation import parse_payload
from principals import PrincipalError, command_as, manifest, tool


class StateError(RuntimeError):
    pass


VOLUMES = {
    "alloy": ("alloy-data",),
    "immich": ("immich-postgres-data", "immich-redis-data", "immich_model-cache"),
    "transmission": ("transmission-data",),
}
CONTROL_ROOT = Path("/var/lib/homelab-service-state")
ACTIVE_STATES = {"running", "paused", "restarting", "stopping"}


def path(value: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or any(c in value for c in "\x00\n\r"):
        raise StateError("state paths must be absolute, single-line metadata")
    result = Path(value)
    if ".." in result.parts or str(result) != value.rstrip("/") or result == Path("/"):
        raise StateError("state paths must be canonical, non-root metadata")
    return result


def below(value: Path, root: Path) -> bool:
    return value == root or root in value.parents


def overlaps(left: Path, right: Path) -> bool:
    return below(left, right) or below(right, left)


@contextmanager
def directory(value: Path):
    """Pin every ancestor without following a mutable symlink, including HOME."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in value.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def metadata(value: Path):
    with directory(value.parent) as descriptor:
        return os.stat(value.name, dir_fd=descriptor, follow_symlinks=False)


def mkdir(value: Path, uid: int, gid: int, mode: int):
    with directory(value.parent) as descriptor:
        try:
            os.mkdir(value.name, mode, dir_fd=descriptor)
        except FileExistsError:
            pass
        child = os.open(value.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            os.fchown(child, uid, gid)
            os.fchmod(child, mode)
        finally:
            os.close(child)


def mountpoints() -> dict[Path, set[str]]:
    result = {}
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        for line in stream:
            fields = line.split()
            value = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
            result[Path(value)] = set(fields[5].split(","))
    return result


def invoke(arguments, *, timeout=600, **kwargs):
    result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=timeout, **kwargs)
    if result.returncode:
        raise StateError("native state operation failed; check declared datasets and required tools")
    return result.stdout


def mounted(value: Path) -> bool:
    return value in mountpoints()


def bind(source: Path, target: Path, readonly: bool):
    with directory(source) as source_fd, directory(target) as target_fd:
        original, current = os.fstat(source_fd), os.fstat(target_fd)
        if mounted(target):
            if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                raise StateError("existing state view has a different source; refusing replacement")
        else:
            if source != target and os.listdir(target_fd):
                raise StateError("state bind target is nonempty; refusing to hide existing data")
            invoke([tool("mount"), "--no-canonicalize", "--bind", f"/proc/self/fd/{source_fd}", f"/proc/self/fd/{target_fd}"],
                   pass_fds=(source_fd, target_fd))
    # The bind qualifier changes only this view, never the physical source mount.
    if readonly and "ro" not in mountpoints().get(target, set()):
        invoke([tool("mount"), "-o", "remount,bind,ro", "--", str(target)])
    elif not readonly and "ro" in mountpoints().get(target, set()):
        raise StateError("writable dataset view is unexpectedly read-only")


def secure_target(role: dict, target: Path):
    home = path(role["home"])
    if not below(target, home) or target == home:
        raise StateError("service views must be strictly within their own private home")
    current = home
    for part in target.relative_to(home).parts[:-1]:
        current /= part
        if mounted(current):
            # Only root-owned protected ancestors are valid. Dataset bind roots
            # may be role-owned, but may not contain another independent view.
            info = metadata(current)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise StateError("state view ancestor is not root-protected")
        else:
            try:
                info = metadata(current)
                if info.st_uid not in (0, role["uid"]) or not stat.S_ISDIR(info.st_mode):
                    raise StateError("state view ancestor has an unexpected identity")
            except FileNotFoundError:
                pass
            mkdir(current, 0, role["gid"], 0o710)
        if current.parent == home and not mounted(current):
            # A mount at the first component also prevents writable HOME from
            # renaming this protected ancestry out from under a later prepare.
            bind(current, current, False)
    if not mounted(target):
        mkdir(target, 0, role["gid"], 0o710)


def tree_entries(root: Path, excluded: set[Path]):
    """FD-relative traversal; no symlink or nested mount can escape the dataset."""
    with directory(root) as descriptor:
        device = os.fstat(descriptor).st_dev
        children = {entry for entry in mountpoints() if entry != root and below(entry, root) and entry not in excluded}
        if children:
            raise StateError("dataset contains an undeclared nested mount")
        for relative, dirs, files, current in os.fwalk(".", dir_fd=descriptor, follow_symlinks=False):
            relative_path = Path(relative)
            dirs[:] = [name for name in dirs if root / relative_path / name not in excluded]
            yield current, None, os.fstat(current)
            for name in dirs + files:
                if root / relative_path / name in excluded:
                    continue
                info = os.stat(name, dir_fd=current, follow_symlinks=False)
                if info.st_dev != device:
                    raise StateError("dataset crosses a filesystem boundary")
                if not stat.S_ISDIR(info.st_mode):
                    yield current, name, info


def reader_acl_needed(descriptor, info):
    """Leave universally accessible files untouched; directories need defaults."""
    if stat.S_ISDIR(info.st_mode):
        return True
    required = 0o555 if info.st_mode & 0o111 else 0o444
    if info.st_mode & required != required:
        return True
    try:
        # O_PATH pins the inode but is not an xattr syscall descriptor.
        os.getxattr(f"/proc/self/fd/{descriptor}", "system.posix_acl_access")
    except OSError as error:
        if error.errno in (errno.ENODATA, errno.ENOTSUP):
            return False
        raise
    # Named entries can deny a reader despite the ordinary mode bits.
    return True


def bounded_links(root: Path, excluded: set[Path], *, reader_only=False):
    links = Counter()
    counts = {}
    mutations = {}
    for current, name, info in tree_entries(root, excluded):
        if name is not None and info.st_nlink > 1:
            key = (info.st_dev, info.st_ino)
            links[key] += 1
            counts[key] = info.st_nlink
            if key not in mutations:
                mutations[key] = not (reader_only and stat.S_ISLNK(info.st_mode))
                if reader_only and not stat.S_ISLNK(info.st_mode):
                    descriptor = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=current)
                    try:
                        pinned = os.fstat(descriptor)
                        if (pinned.st_dev, pinned.st_ino) != key:
                            raise StateError("dataset inode changed during hard-link verification")
                        mutations[key] = reader_acl_needed(descriptor, pinned)
                    finally:
                        os.close(descriptor)
    if any(mutations[key] and links[key] != total for key, total in counts.items()):
        raise StateError("dataset has a hard link outside its declared ownership/ACL boundary")


def reader_acl_batch(reader, permissions, default_directory, descriptors):
    """Keep each target pinned until the native ACL merge has finished."""
    try:
        arguments = [tool("setfacl"), "-m", f"u:{reader}:{permissions}"]
        if default_directory:
            arguments += ["-m", f"d:u:{reader}:r-x"]
        invoke([*arguments, "--", *(f"/proc/self/fd/{descriptor}" for descriptor in descriptors)],
               pass_fds=tuple(descriptors))
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def change_tree(root: Path, *, owner=None, reader=None, excluded=frozenset()):
    """Keep ownership ordered and reader ACL work bounded to four native batches."""
    excluded = set(excluded)
    bounded_links(root, excluded, reader_only=owner is None and reader is not None)
    batches = {}
    workers = {}
    executor = ThreadPoolExecutor(max_workers=4) if reader is not None else None

    def finish_reader_batches():
        completed, _ = wait(workers, return_when=FIRST_COMPLETED)
        for worker in completed:
            workers.pop(worker)
            worker.result()

    def submit_reader_batch(key):
        while len(workers) >= 4:
            finish_reader_batches()
        descriptors = batches.pop(key)
        try:
            worker = executor.submit(reader_acl_batch, reader, *key, descriptors)
        except BaseException:
            for descriptor in descriptors:
                os.close(descriptor)
            raise
        workers[worker] = descriptors

    try:
        for descriptor, name, info in tree_entries(root, excluded):
            if owner is not None:
                if name is None:
                    os.fchown(descriptor, *owner)
                    os.fchmod(descriptor, stat.S_IMODE(info.st_mode))
                elif stat.S_ISREG(info.st_mode):
                    file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
                    try:
                        pinned = os.fstat(file)
                        if (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino):
                            raise StateError("dataset inode changed during ownership adoption")
                        try:
                            capabilities = os.getxattr(file, "security.capability")
                        except OSError as error:
                            if error.errno not in (errno.ENODATA, errno.ENOTSUP):
                                raise
                            capabilities = None
                        os.fchown(file, *owner)
                        os.fchmod(file, stat.S_IMODE(info.st_mode))
                        if capabilities is not None:
                            os.setxattr(file, "security.capability", capabilities)
                    finally:
                        os.close(file)
                else:
                    os.chown(name, *owner, dir_fd=descriptor, follow_symlinks=False)
            if reader is not None and not stat.S_ISLNK(info.st_mode):
                default_directory = stat.S_ISDIR(info.st_mode)
                permissions = "r-x" if default_directory or info.st_mode & 0o111 else "r--"
                # O_PATH also pins special files without opening a FIFO/device.
                file = (os.dup(descriptor) if name is None else
                        os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=descriptor))
                try:
                    pinned = os.fstat(file)
                    if (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino):
                        raise StateError("dataset inode changed during reader ACL preparation")
                    if not reader_acl_needed(file, pinned):
                        os.close(file)
                        continue
                    key = permissions, default_directory
                    batch = batches.setdefault(key, [])
                    batch.append(file)
                except BaseException:
                    os.close(file)
                    raise
                if len(batch) == 64:
                    submit_reader_batch(key)
        for key in tuple(batches):
            submit_reader_batch(key)
        while workers:
            finish_reader_batches()
    finally:
        for descriptors in batches.values():
            for descriptor in descriptors:
                os.close(descriptor)
        for worker, descriptors in workers.items():
            if worker.cancel():
                for descriptor in descriptors:
                    os.close(descriptor)
        if executor is not None:
            # Started native operations finish (or invoke's timeout kills them);
            # cancelled, unstarted operations have their descriptors closed above.
            executor.shutdown(wait=True)


class Preparation:
    def __init__(self, registry):
        self.registry = registry
        self.roles = registry["principals"]
        self.config = registry["statePreparation"]
        self.operator = registry["operator"]
        self.physical = path(registry["physicalStateRoot"])
        self.ha_secret = path(self.config["haSecrets"])
        if self.ha_secret != self.physical / "homeassistant/config/secrets.yaml":
            raise StateError("HA plaintext source is not the declared physical configuration file")
        operator_home = path(self.operator["homeDirectory"])
        self.forbidden = [path(registry["credentialStore"]), path(registry["credentialSeedDirectory"]),
                          operator_home / ".config/keys", operator_home / ".local/share/keyrings",
                          operator_home / ".config/cloudflare", operator_home / ".local/share/cloudflare"]
        self.forbidden += [path(value) for value in self.config.get("forbiddenSources", [])]
        for name, role in self.roles.items():
            account = pwd.getpwnam(role["account"])
            if (role["name"] != name or account.pw_uid != role["uid"] or account.pw_gid != role["gid"]
                    or account.pw_dir != role["home"] or role["uid"] in (0, self.operator["uid"])):
                raise StateError("dedicated service account does not match the immutable registry")
            if not below(path(role["stateRoot"]), path(role["home"])):
                raise StateError("state root is outside its private service home")
        self.excluded = {self.ha_secret}
        self.views = self.make_views()
        self.quiesced = False

    def check_source(self, source: Path):
        if source == path(self.operator["homeDirectory"]) or any(overlaps(source, forbidden) for forbidden in self.forbidden):
            raise StateError("declared view overlaps personal or broker credential storage")

    def make_views(self):
        views = []
        for name, role in self.roles.items():
            for leaf in role["stateDirectories"]:
                if (not isinstance(leaf, str) or not leaf or leaf == "." or Path(leaf).is_absolute()
                        or ".." in Path(leaf).parts or str(Path(leaf)) != leaf
                        or any(c in leaf for c in "\x00\n\r")):
                    raise StateError("stateDirectories must contain bounded relative leaves")
                source = self.physical / leaf
                views.append({"principal": name, "source": str(source),
                              "target": str(path(role["stateRoot"]) / leaf), "writable": True,
                              "create": name in {"tree-dev", "alloy", "metrics", "secrets-export"}})
        views += self.config["views"]
        if "image-updates" in self.roles:
            views.append({"principal": "image-updates", **self.config["imageFeed"], "writable": True,
                          "operatorRead": True})
        if "backrest" in self.roles:
            views += [{"principal": "backrest", **view, "writable": False} for view in self.config["backupViews"]]
        targets = set()
        for view in views:
            source, target = path(view["source"]), path(view["target"])
            self.check_source(source)
            role = self.roles.get(view["principal"])
            if role is None or not below(target, path(role["home"])) or target == path(role["home"]):
                raise StateError("view has no matching enabled private target")
            if target in targets:
                raise StateError("duplicate state view target")
            targets.add(target)
        return views

    def operator_command(self, arguments):
        operator = self.operator
        return [tool("sudo"), "-H", "-u", operator["username"], "--", tool("env"),
                "--chdir=" + operator["homeDirectory"], "-i",
                f"HOME={operator['homeDirectory']}", f"USER={operator['username']}",
                f"LOGNAME={operator['username']}", f"XDG_RUNTIME_DIR=/run/user/{operator['uid']}",
                "PATH=/run/wrappers/bin:/run/current-system/sw/bin", *arguments]

    def runtime_command(self, name, arguments):
        return self.operator_command(arguments) if name is None else command_as(name, arguments)

    def podman(self, name, *arguments, acceptable=(0,), timeout=120):
        result = subprocess.run(self.runtime_command(name, [tool("podman"), *arguments]),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        if result.returncode not in acceptable:
            raise StateError("Podman state metadata operation failed; check runtime availability and declared metadata")
        return result

    def quiesce(self):
        if self.quiesced:
            return
        units = self.config["originalUnits"]
        if any(not isinstance(unit, str) or not re.fullmatch(r"[a-zA-Z0-9_.@-]+\.(service|timer)", unit) for unit in units):
            raise StateError("operator-account workload units must be explicit service/timer metadata")
        # No user manager means no operator-account systemd workload is active.
        # Podman attachment checks still catch containers independent of systemd.
        if Path(f"/run/user/{self.operator['uid']}/bus").exists():
            selected = []
            for unit in units:
                result = subprocess.run(self.operator_command([tool("systemctl"), "--user", "show",
                                          "--property=LoadState,ActiveState", unit]),
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
                values = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
                if values.get("LoadState") == "not-found":
                    continue
                if result.returncode or not values.get("LoadState"):
                    raise StateError("cannot determine declared operator-account workload state")
                selected.append(unit)
            # Stop timers first so none can requeue the explicitly stopped tasks.
            # Include currently inactive declared services too: a timer may have
            # queued one between discovery and the synchronous timer stop.
            for group in ([unit for unit in selected if unit.endswith(".timer")],
                          [unit for unit in selected if unit.endswith(".service")]):
                if group:
                    invoke(self.operator_command([tool("systemctl"), "--user", "stop", *group]), timeout=300)
        self.quiesced = True

    def unattached(self, runtime, roots=(), volumes=()):
        result = self.podman(runtime, "ps", "--all", "--format", "{{.ID}} {{.State}}")
        for line in result.stdout.decode().splitlines():
            identifier, state = line.split(maxsplit=1)
            if state.lower() not in ACTIVE_STATES:
                continue
            mounts = json.loads(self.podman(runtime, "inspect", "--format", "{{json .Mounts}}", identifier).stdout)
            for attachment in mounts or []:
                source = attachment.get("Source")
                if attachment.get("Name") in volumes or (source and any(overlaps(path(source), root) for root in roots)):
                    raise StateError("active source/target container attachment blocks state adoption")

    def marker(self, source: Path, role: dict):
        key = hashlib.sha256(f"{role['uid']}:{source}".encode()).hexdigest()
        return CONTROL_ROOT / (key + ".json")

    def adopted(self, source: Path, role: dict):
        info = metadata(source)
        expected = {"device": info.st_dev, "inode": info.st_ino, "uid": role["uid"], "gid": role["gid"]}
        marker = self.marker(source, role)
        try:
            with directory(marker.parent) as descriptor:
                file = os.open(marker.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
                with os.fdopen(file, "r") as stream:
                    record = json.load(stream)
            return record == expected and info.st_uid == role["uid"] and info.st_gid == role["gid"]
        except FileNotFoundError:
            return False

    def adopt(self, source: Path, role: dict):
        if self.adopted(source, role):
            return
        self.quiesce()
        self.unattached(None, roots=(source,))
        self.unattached(role["name"], roots=(source, path(role["stateRoot"]), path(role["home"])))
        change_tree(source, owner=(role["uid"], role["gid"]), excluded=self.excluded)
        info = metadata(source)
        atomic_file(self.marker(source, role), json.dumps({"device": info.st_dev, "inode": info.st_ino,
                    "uid": role["uid"], "gid": role["gid"]}).encode(), 0, 0)

    def homes(self):
        for role in self.roles.values():
            home = path(role["home"])
            # Host account creation owns the parent; never create arbitrary parents.
            mkdir(home, role["uid"], role["gid"], 0o700)
            secure_target(role, path(role["stateRoot"]))
            bind(path(role["stateRoot"]), path(role["stateRoot"]), False)
            for leaf in (".config", ".cache", ".local"):
                target = home / leaf
                if mounted(target) and metadata(target).st_uid == 0:
                    continue
                mkdir(target, role["uid"], role["gid"], 0o700)
            mkdir(home / ".local/share", role["uid"], role["gid"], 0o700)
            local_state = home / ".local/state"
            if not (mounted(local_state) and metadata(local_state).st_uid == 0):
                mkdir(local_state, role["uid"], role["gid"], 0o700)
            for leaf in ("nix", "homelab", "homelab-nginx", "home-manager"):
                mkdir(home / ".local/state" / leaf, role["uid"], role["gid"], 0o700)
            mkdir(home / ".local/state/home-manager/gcroots", role["uid"], role["gid"], 0o700)

    def prepare_views(self):
        # Ownership first; read-only consumers then receive exact dataset ACLs.
        for view in sorted(self.views, key=lambda view: not view["writable"]):
            role = self.roles[view["principal"]]
            source, target = path(view["source"]), path(view["target"])
            guard = view.get("requireMount")
            if guard and path(guard) not in mountpoints():
                raise StateError("required storage filesystem is absent; refusing fallback directories")
            try:
                with directory(source):
                    pass
            except FileNotFoundError:
                if view.get("optional", False):
                    continue
                if not view.get("create", False):
                    raise StateError("required dataset is missing; application state must be supplied before startup") from None
                self.quiesce()
                if source.parent == self.physical:
                    mkdir(source, 0, role["gid"], 0o700)
                else:
                    raise StateError("only declared top-level nonidentity state leaves may be created")
            if view["writable"] and view.get("adoptOwnership", True):
                self.adopt(source, role)
            elif view["principal"] != "backrest" and not str(source).startswith("/nix/store/"):
                change_tree(source, reader=role["uid"], excluded=self.excluded)
            if view.get("operatorRead", False):
                change_tree(source, reader=self.operator["uid"], excluded=self.excluded)
            secure_target(role, target)
            if not view["writable"] and str(source).startswith("/nix/store/") and mounted(target):
                original, current = metadata(source), metadata(target)
                if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                    # A new generation replaces immutable configuration, not
                    # application data. Never detach an active consumer or RW view.
                    if "ro" not in mountpoints()[target]:
                        raise StateError("immutable configuration view is unexpectedly writable")
                    self.unattached(view["principal"], roots=(target,))
                    invoke([tool("umount"), "--", str(target)])
            bind(source, target, not view["writable"])

    def backup_access(self):
        """Reconcile backup readers without blocking unrelated workload startup."""
        if "backrest" not in self.roles:
            return
        reader = self.roles["backrest"]["uid"]
        for view in self.views:
            if view["principal"] != "backrest" or view["writable"] or str(view["source"]).startswith("/nix/store/"):
                continue
            source = path(view["source"])
            guard = view.get("requireMount")
            if guard and path(guard) not in mountpoints():
                raise StateError("required storage filesystem is absent; refusing fallback directories")
            try:
                with directory(source):
                    pass
            except FileNotFoundError:
                if view.get("optional", False):
                    continue
                raise StateError("required backup dataset is missing") from None
            change_tree(source, reader=reader, excluded=self.excluded)

    def volume_path(self, runtime, name):
        source = path(json.loads(self.podman(runtime, "volume", "inspect", "--format", "{{json .Mountpoint}}", name).stdout))
        driver = self.podman(runtime, "volume", "inspect", "--format", "{{.Driver}}", name).stdout.strip()
        home = path(self.operator["homeDirectory"] if runtime is None else self.roles[runtime]["home"])
        if driver != b"local" or not below(source, home) or source.parts[-3:] != ("volumes", name, "_data"):
            raise StateError("named volume mountpoint is outside its declared private local runtime")
        self.check_source(source)
        return source

    def volumes(self):
        for name, volumes in VOLUMES.items():
            if name not in self.roles:
                continue
            role = self.roles[name]
            for volume in volumes:
                target_exists = self.podman(name, "volume", "exists", volume, acceptable=(0, 1)).returncode == 0
                if not target_exists:
                    self.quiesce()
                    self.unattached(name, volumes=(volume,))
                    self.podman(name, "volume", "create", volume)
                target = self.volume_path(name, volume)
                with directory(target) as descriptor:
                    nonempty = bool(os.listdir(descriptor))
                if nonempty:
                    # A nonempty target is authoritative; never overwrite it with source data.
                    self.adopt(target, role)
                    continue
                self.quiesce()
                self.unattached(None, volumes=(volume,))
                self.unattached(name, volumes=(volume,), roots=(target,))
                if self.podman(None, "volume", "exists", volume, acceptable=(0, 1)).returncode:
                    raise StateError("required source named volume is missing; refusing an empty replacement")
                source = self.volume_path(None, volume)
                with directory(source) as descriptor:
                    if not os.listdir(descriptor):
                        raise StateError("required source named volume is empty; refusing a replacement store")
                copy_empty(source, target, role)
                self.adopt(target, role)

    def tree_dev(self):
        if "tree-dev" not in self.roles:
            return
        role = self.roles["tree-dev"]
        details = self.config["treeDev"]
        source, target = path(details["sessionSource"]), path(details["sessionTarget"])
        self.check_source(source)
        if target != path(role["stateRoot"]) / "tree-eclass-dev":
            raise StateError("Tree dev seed target is not its declared independent state")
        for name in ("session", "code"):
            with directory(target) as descriptor:
                try:
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                        raise StateError("existing Tree dev identity is not a nonempty regular file")
                    continue
                except FileNotFoundError:
                    pass
            self.quiesce()
            self.unattached(None, roots=(source,))
            self.unattached("tree-dev", roots=(target,))
            copy_identity(source / name, target / name, role)
        checkout = path(details["checkoutTarget"])
        original = path(details["checkoutSource"])
        self.check_source(original)
        if not below(checkout, path(role["home"])):
            raise StateError("Tree dev checkout must be independent in its own private home")
        secure_target(role, checkout)
        with directory(checkout) as descriptor:
            empty = not os.listdir(descriptor)
        if empty:
            self.quiesce()
            self.unattached(None, roots=(original,))
            self.unattached("tree-dev", roots=(checkout,))
            copy_empty(original, checkout, role)
        self.adopt(checkout, role)
        # A self-bind makes the independent writable checkout unreplaceable at HOME.
        bind(checkout, checkout, False)

    def images(self):
        for details in self.config["originalTreeImages"]:
            name, image = details["principal"], details["image"]
            if name not in self.roles:
                continue
            if image not in ("localhost/tree-eclass:stable", "localhost/tree-eclass:dev"):
                raise StateError("source Tree image must be an explicit local stable/dev reference")
            if not self.podman(name, "image", "exists", image, acceptable=(0, 1)).returncode:
                continue
            if self.podman(None, "image", "exists", image, acceptable=(0, 1)).returncode:
                if details.get("optional", name == "tree-dev"):
                    continue
                raise StateError("required source production Tree image is missing; refusing replacement provenance")
            expected = self.podman(None, "image", "inspect", "--format", "{{.Id}}", image).stdout.strip()
            producer = consumer = None
            try:
                producer = subprocess.Popen(self.operator_command([tool("podman"), "save", "--format=oci-archive", image]),
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                consumer = subprocess.Popen(command_as(name, [tool("podman"), "load"]), stdin=producer.stdout,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                producer.stdout.close()
                loaded, saved = consumer.wait(timeout=1800), producer.wait(timeout=1800)
                if loaded or saved:
                    raise StateError("Tree OCI transfer failed; source image retained")
            finally:
                for process in (consumer, producer):
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait()
                if producer is not None and producer.stdout is not None:
                    producer.stdout.close()
            actual = self.podman(name, "image", "inspect", "--format", "{{.Id}}", expected.decode()).stdout.strip()
            if actual.removeprefix(b"sha256:") != expected.removeprefix(b"sha256:"):
                raise StateError("OCI import changed source Tree image identity")
            self.podman(name, "tag", expected.decode(), image)

    def metrics(self):
        root = path(self.registry["metricsDirectory"])
        mkdir(root, 0, 0, 0o755)
        for role in self.roles.values():
            mkdir(root / role["name"], role["uid"], role["gid"], 0o700)
            if "alloy" in self.roles:
                change_tree(root / role["name"], reader=self.roles["alloy"]["uid"])

    def prepare(self):
        self.homes()
        # Remove any inherited backup ACL before publishing read-only data views.
        self.private_ha_placeholder(remove=False)
        self.prepare_views()
        self.volumes()
        self.tree_dev()
        self.images()
        self.metrics()

    def private_ha_placeholder(self, *, remove):
        if "homeassistant" not in self.roles:
            return
        source = self.ha_secret
        with directory(source.parent) as parent:
            try:
                descriptor = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                descriptor = None
            try:
                if descriptor is not None:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise StateError("HA plaintext secrets must be a bounded private regular file")
                    if mounted(source) and "ro" in mountpoints()[source]:
                        if info.st_size or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
                            raise StateError("mounted HA placeholder has unexpected ownership or content")
                        if remove:
                            self.protect_ha_view(descriptor)
                        return
                    os.fchmod(descriptor, 0o600)
                    invoke([tool("setfacl"), "-b", "--", f"/proc/self/fd/{descriptor}"], pass_fds=(descriptor,))
                    if info.st_size:
                        if not remove:
                            return
                        self.quiesce()
                        self.unattached(None, roots=(source.parent,))
                        self.unattached("homeassistant", roots=(source.parent,
                            path(self.roles["homeassistant"]["stateRoot"]) / "homeassistant/config"))
                        payload, digest = read_record("homeassistant")
                        if not payload or not digest:
                            raise StateError("verified encrypted HA record is missing; plaintext retained")
                        expected = parse_payload("homeassistant", payload)["secrets.yaml"].encode("utf-8")
                        chunks = []
                        while chunk := os.read(descriptor, 65536):
                            chunks.append(chunk)
                        original = b"".join(chunks)
                        source_recovery = None
                        if not hmac.compare_digest(original, expected):
                            # An unused physical copy may differ from the runtime
                            # overlay. Removal requires a verified encrypted snapshot
                            # containing exactly the same bytes as the physical copy.
                            snapshot = path(self.registry["credentialSeedDirectory"]) / ".homeassistant-physical.cred"
                            crypto = NativeCrypto()
                            try:
                                encrypted = private_read(snapshot, owner=self.operator["uid"])
                                saved = crypto.seed_decrypt("homeassistant", encrypted)
                                decrypted = parse_payload("homeassistant", saved)["secrets.yaml"].encode("utf-8")
                                if (not hmac.compare_digest(original, decrypted)
                                        or not hmac.compare_digest(saved, crypto.seed_decrypt("homeassistant", encrypted))):
                                    raise StateError("HA physical secrets differ from encrypted source snapshot; plaintext retained")
                                source_recovery = crypto.export(self.registry["backup"]["ageRecipient"], saved)
                            except (OSError, BrokerError):
                                raise StateError("HA physical secrets lack a verified encrypted source snapshot; plaintext retained") from None
                        ciphertext = export_record("homeassistant")
                        if not ciphertext or not ciphertext.startswith(b"age-encryption.org/v1\n"):
                            raise StateError("HA encrypted backup export unavailable; plaintext retained")
                        exporter = self.roles.get("secrets-export")
                        if exporter is None:
                            raise StateError("HA plaintext removal requires the encrypted backup exporter")
                        recovery = path(exporter["stateRoot"]) / "secrets-export/homeassistant.json.age"
                        atomic_file(recovery, ciphertext, exporter["uid"], exporter["gid"])
                        if source_recovery is not None:
                            atomic_file(recovery.with_name("homeassistant-physical.json.age"),
                                        source_recovery, exporter["uid"], exporter["gid"])
                        if "backrest" in self.roles:
                            change_tree(recovery.parent, reader=self.roles["backrest"]["uid"])
                        # Broker reads decrypt its own user-scoped ciphertext each time.
                        # A second read checks the encrypted record did not change while exporting.
                        verified, verified_digest = read_record("homeassistant")
                        if verified_digest != digest or not hmac.compare_digest(verified or b"", payload):
                            raise StateError("HA encrypted record changed during verification; plaintext retained")
                        current = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
                        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                            raise StateError("HA source changed during verification; plaintext retained")
                        if mounted(source):
                            raise StateError("nonempty HA plaintext is unexpectedly mounted; refusing destructive replacement")
                        os.unlink(source.name, dir_fd=parent)
                        os.fsync(parent)
                elif not remove:
                    return
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            if not remove:
                return
            descriptor = os.open(source.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600,
                                 dir_fd=parent) if not source.exists() else os.open(
                                     source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if os.fstat(descriptor).st_size:
                    raise StateError("HA plaintext placeholder changed; refusing replacement")
                os.fchown(descriptor, 0, 0)
                os.fchmod(descriptor, 0o600)
                invoke([tool("setfacl"), "-b", "--", f"/proc/self/fd/{descriptor}"], pass_fds=(descriptor,))
                os.fsync(descriptor)
                if not mounted(source):
                    invoke([tool("mount"), "--no-canonicalize", "--bind", f"/proc/self/fd/{descriptor}",
                            f"/proc/self/fd/{parent}/{source.name}"], pass_fds=(descriptor, parent))
                self.protect_ha_view(descriptor)
            finally:
                os.close(descriptor)
        if "ro" not in mountpoints().get(source, set()):
            invoke([tool("mount"), "-o", "remount,bind,ro", "--", str(source)])

    def protect_ha_view(self, descriptor: int):
        target = path(self.roles["homeassistant"]["stateRoot"]) / "homeassistant/config/secrets.yaml"
        with directory(target.parent) as parent:
            target_info = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
            source_info = os.fstat(descriptor)
            if (target_info.st_dev, target_info.st_ino) != (source_info.st_dev, source_info.st_ino):
                raise StateError("HA private view does not contain the verified physical placeholder")
            if not mounted(target):
                invoke([tool("mount"), "--no-canonicalize", "--bind", f"/proc/self/fd/{descriptor}",
                        f"/proc/self/fd/{parent}/{target.name}"], pass_fds=(descriptor, parent))
        if "ro" not in mountpoints().get(target, set()):
            invoke([tool("mount"), "-o", "remount,bind,ro", "--", str(target)])


def atomic_file(destination: Path, value: bytes, uid: int, gid: int):
    with directory(destination.parent) as parent:
        descriptor, temporary = tempfile.mkstemp(prefix=".homelab-state-", dir=f"/proc/self/fd/{parent}")
        name = Path(temporary).name
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchown(stream.fileno(), uid, gid)
                os.fchmod(stream.fileno(), 0o600)
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


def copy_empty(source: Path, target: Path, role: dict):
    # Copying creates new inodes, but nested mounts remain outside the scope.
    for _ in tree_entries(source, set()):
        pass
    if mounted(target):
        raise StateError("empty copy target is already mounted; refusing data replacement")
    with directory(target.parent) as parent, directory(source) as original, directory(target) as existing:
        if os.listdir(existing):
            return
        temporary = tempfile.mkdtemp(prefix=".homelab-import-", dir=f"/proc/self/fd/{parent}")
        staged = target.parent / Path(temporary).name
        # Staging is retained on failure rather than replaying a partial source over
        # a live/nonempty target. Only the explicitly empty destination is removed.
        with directory(staged) as destination:
            invoke([tool("cp"), "--archive", "--reflink=auto", "--one-file-system", "--",
                    f"/proc/self/fd/{original}/.", f"/proc/self/fd/{destination}/"],
                   pass_fds=(original, destination), timeout=3600)
        change_tree(staged, owner=(role["uid"], role["gid"]))
        if os.listdir(existing):
            raise StateError("copy destination became nonempty; source and staging retained")
        current = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
        pinned = os.fstat(existing)
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise StateError("copy destination changed; source and staging retained")
        os.rmdir(target.name, dir_fd=parent)
        os.rename(staged.name, target.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)


def copy_identity(source: Path, target: Path, role: dict):
    with directory(source.parent) as parent, directory(target.parent) as destination:
        descriptor = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not info.st_size or info.st_nlink != 1:
                raise StateError("required source Tree identity is missing or outside its private boundary")
            output, temporary = tempfile.mkstemp(prefix=".homelab-identity-", dir=f"/proc/self/fd/{destination}")
            name = Path(temporary).name
            try:
                invoke([tool("cp"), "--preserve=all", "--reflink=auto", "--",
                        f"/proc/self/fd/{descriptor}", f"/proc/self/fd/{output}"],
                       pass_fds=(descriptor, output))
                os.fchown(output, role["uid"], role["gid"])
                os.fchmod(output, 0o600)
                os.fsync(output)
                # link is an atomic no-overwrite publication; a concurrent trusted
                # selection/identity is never replaced by this source seed.
                os.link(name, target.name, src_dir_fd=destination, dst_dir_fd=destination,
                        follow_symlinks=False)
                os.fsync(destination)
            finally:
                os.close(output)
                os.unlink(name, dir_fd=destination)
        finally:
            os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "protect", "backup-access"))
    arguments = parser.parse_args(argv)
    try:
        if os.getuid() != 0 or os.geteuid() != 0:
            raise StateError("service state preparation/protection requires root")
        mkdir(CONTROL_ROOT, 0, 0, 0o700)
        with directory(CONTROL_ROOT) as parent:
            lock = "backup-access.lock" if arguments.operation == "backup-access" else "lock"
            descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                preparation = Preparation(manifest())
                if arguments.operation == "prepare":
                    preparation.prepare()
                elif arguments.operation == "protect":
                    preparation.private_ha_placeholder(remove=True)
                else:
                    preparation.backup_access()
            finally:
                os.close(descriptor)
        return 0
    except StateError as error:
        print("Service state blocked: " + str(error), file=sys.stderr)
    except (OSError, ValueError, KeyError, CredentialError, PrincipalError, subprocess.SubprocessError):
        # Native failures and invalid records never include stdout, plaintext,
        # credential fields, environment values or exception detail in logs.
        print("Service state blocked: required native operation, declared state or immutable metadata unavailable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
