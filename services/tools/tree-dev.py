#!/usr/bin/env python3
"""Branch a disposable dev stack off production tree-eclass, or promote to prod.

Shape contract (homelab-owned): prod (`uni`, `:stable`) and staging
(`staging.uni`, `:dev`) run in parallel. `up` branches prod sqlite.db +
objects into the independent dev state dir, migrates the branch and serves it
on staging; prod keeps serving throughout and is never stopped, hijacked or
restored by these commands. `sync` rebuilds :dev from the working checkout
(dirty allowed), re-branches from live prod, migrates the new branch and swaps
staging onto it; a migration failure discards the new branch and restores the
previous one, so the failure is always observed before the branch serves. `down`
discards the branch; staging answers 502 until the next `up`, prod is
untouched. `stage` warms the declared upstream FROM images via
`homelab-image-update inputs` so their seven-day base observation can
elapse. `deploy` authorizes only seven-day-aged immutable bases, then builds
(or reuses) the clean committed HEAD image from a pinned rendering with
`--pull=never` and promotes it by immutable ID (atomic :stable swap, no
homelab edit for releases); the staging stack is untouched and may keep
running. A cold durable checkpoint and retained image recover both code and
data on failure. Interrupted releases remain blocked until `recover`. Locally
built code is never age-gated.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
import random
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import image_build
from image_build import BuildError, BuildTarget, REVISION_LABEL, git_rev
from principals import (
    PrincipalError,
    manifest,
    principal,
    principal_for_unit,
    require_admin,
    run_as,
    tool,
)
from tree_recovery import RecoveryError, RecoveryStore

# One error type across the shared build pipeline and the Tree lifecycle.
DevError = BuildError

LABEL_NAMESPACE = "io.homelab.tree"
DEPLOY_MIGRATION_UNIT = "tree-eclass-deploy-migrate.service"
PRODUCTION_UNITS = (DEPLOY_MIGRATION_UNIT, "tree-eclass.service", "tree-eclass-migrate.service")
PRODUCTION_CONTAINERS = ("tree-eclass", "tree-eclass-migrate", "tree-eclass-deploy-migrate")


def build_target() -> BuildTarget:
    return BuildTarget(
        name="tree-eclass",
        repository="localhost/tree-eclass",
        checkout=Path(os.environ["HOMELAB_TREE_CHECKOUT"]),
        dockerfile="Dockerfile",
        builder="tree-dev",
        owner="tree-eclass",
        label_namespace=LABEL_NAMESPACE,
        markers=("Dockerfile", "backend/go.mod"),
        revision_build_arg=None,
    )


def recovery_store() -> RecoveryStore:
    return RecoveryStore(Path(os.environ.get("HOMELAB_TREE_RECOVERY", "/var/lib/homelab/tree-releases")))


def prod_state() -> Path:
    return Path(principal("tree-eclass")["stateRoot"]) / "tree-eclass"


def dev_state() -> Path:
    return Path(principal("tree-dev")["stateRoot"]) / "tree-eclass-dev"


def systemctl(*arguments: str, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        roles = {principal_for_unit(arg) for arg in arguments if arg.endswith(".service")}
        if len(roles) != 1:
            raise DevError("systemctl operation requires one declared Tree principal")
        result = run_as(
            roles.pop(),
            [tool("systemctl"), "--user", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except OSError as error:
        raise DevError(f"could not run systemctl --user: {error}") from error
    return result


def active(unit: str) -> bool:
    result = systemctl("show", "--property=ActiveState", "--value", unit)
    return result.returncode == 0 and result.stdout.strip() in (
        "active",
        "reloading",
        "activating",
    )


def wait_unit(unit: str, want_active: bool, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if active(unit) == want_active:
            return
        time.sleep(2)
    state = "active" if want_active else "inactive"
    raise DevError(f"{unit} did not become {state} within {timeout}s")


def container_health(name: str) -> bool:
    """Probe /api/health inside the container's actual principal runtime."""
    result = podman(
        "exec",
        name,
        "python3",
        "-c",
        "import urllib.request,json,sys; "
        "body=json.load(urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=10)); "
        "sys.exit(0 if body.get('status') == 'ok' else 1)",
        timeout=30,
        role="tree-dev" if name == "tree-eclass-dev" else "tree-eclass",
    )
    return result.returncode == 0


def wait_health(name: str, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if container_health(name):
            return
        time.sleep(2)
    raise DevError(f"{name} did not answer /api/health within timeout")


def checkout_root() -> Path:
    try:
        return image_build.checkout_root(build_target())
    except BuildError:
        raise DevError("HOMELAB_TREE_CHECKOUT must name the tree-eclass checkout") from None


def podman(*arguments: str, timeout: int = 600, role: str = "tree-dev") -> subprocess.CompletedProcess:
    return image_build.podman(role, *arguments, timeout=timeout)


def image_exists(tag: str) -> bool:
    return image_build.image_exists("tree-dev", build_target().reference(tag))


def dev_image_rev() -> str | None:
    """Revision baked into :dev, or None when the tag is absent or unlabeled.

    Tag existence is not a freshness signal: a :dev built days ago silently
    predates the checkout and its embedded migrations then refuse the branched
    database. Callers rebuild whenever this differs from the checkout HEAD.
    """
    result = podman(
        "image", "inspect", "--format", f'{{{{ index .Labels "{REVISION_LABEL}" }}}}', "localhost/tree-eclass:dev",
        timeout=60,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def build_image(root: Path, tag: str, rev: str) -> None:
    """Rebuild :dev immediately from the working checkout (dirty allowed)."""
    target = build_target()
    with tempfile.TemporaryDirectory(prefix="tree-dev-inputs-") as temporary:
        specification, rendered = image_build.authorize_bases(
            target, root, require_aged=False, directory=Path(temporary)
        )
        image_build.build_image(target, root, tag, rev, specification, rendered, committed=False)


def stop_prod() -> bool:
    """Stop prod if running. Returns True if it was running (to restore on down)."""
    running = active("tree-eclass.service")
    if running:
        result = systemctl("stop", "tree-eclass.service")
        if result.returncode != 0:
            raise DevError("could not stop tree-eclass.service")
        wait_unit("tree-eclass.service", False)
    return running


def start_prod() -> None:
    result = systemctl("start", "tree-eclass.service")
    if result.returncode != 0:
        raise DevError("could not start tree-eclass.service")
    wait_unit("tree-eclass.service", True)


def production_running() -> bool:
    result = systemctl("show", "--property=ActiveState", "--value", "tree-eclass.service")
    state = result.stdout.strip()
    if result.returncode or state not in ("active", "reloading", "activating", "inactive", "failed", "deactivating"):
        raise DevError("could not determine production runtime state; refusing deployment")
    return state in ("active", "reloading", "activating", "deactivating")


def production_containers() -> list[dict]:
    listed = podman("ps", "--all", "--quiet", role="tree-eclass", timeout=30)
    if listed.returncode:
        raise DevError("could not enumerate production containers")
    identifiers = listed.stdout.split()
    if not identifiers:
        return []
    inspected = podman("container", "inspect", *identifiers, role="tree-eclass", timeout=30)
    if inspected.returncode:
        raise DevError("could not inspect every production container; writers are not proven stopped")
    containers = json.loads(inspected.stdout)
    if not isinstance(containers, list) or len(containers) != len(identifiers):
        raise DevError("incomplete production container inspection")
    for container in containers:
        if not isinstance(container, dict) or not isinstance(container.get("State"), dict):
            raise DevError("invalid production container inspection")
        if not isinstance(container["State"].get("Running"), bool):
            raise DevError("unknown production container running state")
        if not isinstance(container.get("Name"), str) or not isinstance(container.get("Id"), str):
            raise DevError("production container inspection is missing identity")
        mounts = container.get("Mounts")
        if not isinstance(mounts, list) or any(
            not isinstance(mount, dict)
            or not isinstance(mount.get("Source"), str)
            or not isinstance(mount.get("RW"), bool)
            for mount in mounts
        ):
            raise DevError("production container inspection is missing mount permissions")
    return containers


def writing_container(container: dict, state: Path) -> bool:
    live = container["State"]["Running"] or container["State"].get("Restarting", False)
    if not live:
        return False
    if container["Name"].lstrip("/") in PRODUCTION_CONTAINERS:
        return True
    for mount in container["Mounts"]:
        if not mount["RW"]:
            continue
        source = Path(mount["Source"]).resolve()
        if source == state or state in source.parents or source in state.parents:
            return True
    return False


def stop_production_writers(state: Path) -> None:
    """After permit revocation, stop known writers and prove all state writers cold."""
    failures = []
    for unit in PRODUCTION_UNITS:
        try:
            stopped = systemctl("stop", unit)
            if stopped.returncode:
                raise DevError(f"could not stop production writer {unit}: {stopped.stderr.strip()[-1000:]}")
            deadline = time.monotonic() + 180
            while True:
                probe = systemctl("show", "--property=ActiveState", "--value", unit)
                if probe.returncode:
                    raise DevError(f"could not prove {unit} stopped")
                observed = probe.stdout.strip()
                if observed in ("inactive", "failed"):
                    if observed == "failed" and unit == DEPLOY_MIGRATION_UNIT:
                        cleared = systemctl("reset-failed", unit)
                        if cleared.returncode:
                            raise DevError(f"could not clear stopped migration unit: {cleared.stderr.strip()[-1000:]}")
                    break
                if observed not in ("active", "reloading", "activating", "deactivating"):
                    raise DevError(f"unknown state for production writer {unit}: {observed!r}")
                if time.monotonic() >= deadline:
                    raise DevError(f"production writer {unit} did not stop")
                time.sleep(2)
        except (PrincipalError, DevError, OSError, subprocess.SubprocessError) as error:
            failures.append(str(error))
    # A failed user-manager probe is not permission to keep an orphaned writer
    # alive: still attempt all known container stops, but never infer coldness.
    try:
        stopped = podman("stop", "--ignore", "--time", "30", *PRODUCTION_CONTAINERS, role="tree-eclass", timeout=180)
        if stopped.returncode:
            raise DevError(f"could not stop lingering production containers: {stopped.stderr[-1000:]}")
        state = state.resolve()
        remaining = [container["Name"] for container in production_containers() if writing_container(container, state)]
        if remaining:
            raise DevError(
                f"production writers remain live: {', '.join(remaining)}; "
                "resolve those writers before retrying `tree-dev recover`"
            )
    except (PrincipalError, DevError, OSError, ValueError, subprocess.SubprocessError) as error:
        failures.append(str(error))
    if failures:
        raise DevError("; ".join(failures))
    removed = podman(
        "rm", "--ignore", "tree-eclass-migrate", "tree-eclass-deploy-migrate", role="tree-eclass", timeout=60
    )
    if removed.returncode:
        raise DevError(f"could not remove stopped migration containers: {removed.stderr[-1000:]}")


def migrate_production(store: RecoveryStore, record: dict) -> None:
    store.allow_migration(record)
    guidance = f"see journalctl --user -u {DEPLOY_MIGRATION_UNIT} under the tree-eclass principal"
    result = systemctl("start", DEPLOY_MIGRATION_UNIT, timeout=720)
    if result.returncode:
        raise DevError(f"production migration failed:\n{result.stderr[-4000:]}\n{guidance}")
    observed = systemctl(
        "show",
        "--property=LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus",
        DEPLOY_MIGRATION_UNIT,
    )
    properties = dict(line.split("=", 1) for line in observed.stdout.splitlines() if "=" in line)
    if observed.returncode or properties.get("LoadState") != "loaded":
        raise DevError(f"production migration unit was lost or unavailable; {guidance}")
    if any(
        properties.get(key) != value
        for key, value in {
            "ActiveState": "active",
            "SubState": "exited",
            "Result": "success",
            "ExecMainCode": "1",
            "ExecMainStatus": "0",
        }.items()
    ):
        raise DevError(f"production migration completion could not be proven; {guidance}")
    # Revoke the mandatory unit EnvironmentFile before stopping its cgroup.
    # A delayed StartUnit can no longer create a writer after the cold proof.
    store.block(record)
    stop_production_writers(Path(record["state"]))


def run_oneshot(unit: str) -> None:
    result = systemctl("start", unit)
    if result.returncode != 0:
        raise DevError(f"could not start {unit}: {result.stderr.strip()[-1000:]}")
    deadline = time.time() + 600
    while time.time() < deadline:
        probe = systemctl("show", "--property=ActiveState,SubState,Result", "--value", unit)
        lines = probe.stdout.splitlines()
        if len(lines) >= 3 and lines[0] == "active" and lines[1] == "exited":
            if lines[2] not in ("success", ""):
                raise DevError(f"{unit} exited with result {lines[2]}")
            return
        if len(lines) >= 1 and lines[0] == "failed":
            raise DevError(f"{unit} failed; see journalctl --user -u {unit}")
        time.sleep(3)
    raise DevError(f"{unit} did not finish within 600s")


def branch_database(prod_db: Path, dev_db: Path) -> None:
    """Trusted stopped-production backup; only the disposable destination changes."""
    if prod_db.parent.is_symlink() or prod_db.is_symlink() or not prod_db.is_file():
        raise DevError(f"production database is missing: {prod_db}")
    private_dev_directory(dev_db.parent)
    with tempfile.NamedTemporaryFile(prefix=".sqlite-branch-", dir=dev_db.parent, delete=False) as file:
        temporary = Path(file.name)
    try:
        with (
            closing(sqlite3.connect(prod_db.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(temporary)) as destination,
        ):
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise DevError("branched database failed integrity_check")
            if destination.execute("SELECT count(*) FROM tree_go_migrations").fetchone()[0] == 0:
                raise DevError("branched database has an empty migration ledger")
            destination.execute("VACUUM")
        own_dev(temporary)
        for suffix in ("-wal", "-shm", "-journal"):
            dev_db.with_name(dev_db.name + suffix).unlink(missing_ok=True)
        temporary.replace(dev_db)
    finally:
        temporary.unlink(missing_ok=True)


def sync_objects(prod_objects: Path, dev_objects: Path) -> None:
    """Copy production objects via trusted host management, never a runtime mount."""
    if prod_objects.is_symlink() or not prod_objects.is_dir():
        raise DevError("production objects directory is missing")
    private_dev_directory(dev_objects)
    copy_private_tree(prod_objects, dev_objects)


def own_dev(path: Path) -> None:
    identity = principal("tree-dev")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise DevError("Tree branch ownership requires a regular file/directory")
        os.fchown(descriptor, identity["uid"], identity["gid"])
        os.fchmod(descriptor, 0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
    finally:
        os.close(descriptor)


def private_dev_directory(path: Path) -> None:
    root = dev_state()
    if path != root and root not in path.parents:
        raise DevError("refusing a destination outside disposable dev state")
    if root.is_symlink() or any(
        part.is_symlink() for part in (path, *path.parents) if part == root or root in part.parents
    ):
        raise DevError("dev state must not contain symlink destinations")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    own_dev(path)


def copy_private_tree(source: Path, destination: Path) -> None:
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_symlink() or target.is_symlink():
            raise DevError("Tree state copy refuses symlinks")
        if entry.is_dir():
            private_dev_directory(target)
            copy_private_tree(entry, target)
        elif entry.is_file():
            link_private_file(entry, target, destination)
        else:
            raise DevError("Tree state copy requires regular files/directories")


def link_private_file(source: Path, target: Path, directory: Path) -> None:
    """Share an immutable object via hardlink; fall back to a private copy.

    Objects are content-addressed and immutable, so a link is safe and makes
    re-branching on every `sync` cheap: staging writes land on new inodes and
    prod inodes are never mutated through the staging tree. A link is only
    kept when the shared inode already carries the exact dev ownership and
    mode; otherwise the file is copied privately (also the EXDEV fallback),
    because chmod/chown through a link would mutate the production inode.
    """
    identity = principal("tree-dev")
    try:
        os.link(source, target)
    except OSError:
        copy_private_file(source, target, directory, identity)
        return
    try:
        info = os.stat(target, follow_symlinks=False)
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (identity["uid"], identity["gid"], 0o600):
            raise OSError("shared inode needs different ownership")
    except OSError:
        target.unlink()
        copy_private_file(source, target, directory, identity)


def copy_private_file(source: Path, target: Path, directory: Path, identity: dict) -> None:
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".object-", delete=False) as output:
        temporary = Path(output.name)
        try:
            with source.open("rb") as original:
                shutil.copyfileobj(original, output)
            output.flush()
            os.fchown(output.fileno(), identity["uid"], identity["gid"])
            os.fchmod(output.fileno(), 0o600)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def write_text(path: Path, content: str) -> None:
    private_dev_directory(path.parent)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.name}-", delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(content)
            output.flush()
            identity = principal("tree-dev")
            os.fchown(output.fileno(), identity["uid"], identity["gid"])
            os.fchmod(output.fileno(), 0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def set_tree_route(dev: bool) -> None:
    """Only trusted management changes nginx's own numeric route override."""
    nginx = principal("nginx")
    path = Path(nginx["home"]) / ".local/state/homelab-nginx/tree-upstream.conf"
    content = f"set $up_tree 127.0.0.1:{manifest()['ports']['tree-dev']};\n" if dev else ""
    script = (
        "import os,pathlib,sys,tempfile\n"
        "p=pathlib.Path(sys.argv[1]); value=sys.argv[2]\n"
        "if p.is_symlink(): raise SystemExit('route include must not be a symlink')\n"
        "old=p.read_text(); print(old,end='')\n"
        "with tempfile.NamedTemporaryFile(mode='w',dir=p.parent,delete=False) as f:\n"
        " f.write(value); f.flush(); os.fchmod(f.fileno(),0o600); name=f.name\n"
        "os.replace(name,p)\n"
    )
    result = run_as(
        "nginx", [tool("python"), "-c", script, str(path), content], text=True, capture_output=True, timeout=30
    )
    if result.returncode:
        raise DevError("could not update nginx's Tree route include")
    previous = result.stdout
    restarted = run_as(
        "nginx", [tool("systemctl"), "--user", "restart", "nginx.service"], text=True, capture_output=True, timeout=120
    )
    if restarted.returncode:
        run_as("nginx", [tool("python"), "-c", script, str(path), previous], check=True, timeout=30)
        run_as("nginx", [tool("systemctl"), "--user", "restart", "nginx.service"], timeout=120)
        raise DevError("nginx route switch failed; previous route restored")


def clear_tree_route() -> None:
    """Defensively empty the legacy single-route hijack so `uni` means prod.

    Staging is served by its own static server block and never needs the
    override. A stale override (from the pre-staging flow) would steal `uni`;
    clear it here. nginx is restarted only when the file was non-empty, so
    steady-state `up`/`down` never flap prod traffic.
    """
    nginx = principal("nginx")
    path = Path(nginx["home"]) / ".local/state/homelab-nginx/tree-upstream.conf"
    check = run_as("nginx", [tool("cat"), str(path)], text=True, capture_output=True, timeout=30)
    if check.returncode:
        raise DevError("could not read nginx's Tree route include")
    if not check.stdout.strip():
        return
    set_tree_route(False)


def new_branch_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{random.randrange(16**6):06x}"


def branch_from_prod(prod: Path, dev: Path, rev: str) -> str:
    """Copy live prod identity, database and objects into the dev branch."""
    branch = new_branch_id()
    print(f"Branching production data into {branch} ...")
    for name in ("session", "code"):
        source = prod / name
        if source.is_symlink() or not source.is_file() or not source.read_text().strip():
            raise DevError(f"required production {name} is missing")
        write_text(dev / name, source.read_text())
    branch_database(prod / "jobs" / "sqlite.db", dev / "jobs" / "sqlite.db")
    sync_objects(prod / "objects", dev / "objects")
    write_text(dev / "branch", branch + "\n")
    write_text(dev / "rev", rev + "\n")
    return branch


def wipe_branch(dev: Path) -> None:
    """Discard the dev branch data. Prod is never touched."""
    for directory in ("jobs", "objects"):
        private_dev_directory(dev / directory)
    for name in ("sqlite.db", "sqlite.db-wal", "sqlite.db-shm", "sqlite.db-journal"):
        (dev / "jobs" / name).unlink(missing_ok=True)
    objects = dev / "objects"
    if objects.is_dir():
        for path in objects.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    for name in ("branch", "rev", "session", "code"):
        try:
            (dev / name).unlink()
        except FileNotFoundError:
            pass


def stop_dev_stack() -> bool:
    """Stop the dev stack and its migrate unit. Returns whether it ran."""
    running = active("tree-eclass-dev.service")
    if running:
        result = systemctl("stop", "tree-eclass-dev.service")
        if result.returncode != 0:
            raise DevError("could not stop tree-eclass-dev.service")
        wait_unit("tree-eclass-dev.service", False)
    systemctl("stop", "tree-eclass-dev-migrate.service")
    return running


def start_dev_stack() -> None:
    result = systemctl("start", "tree-eclass-dev.service")
    if result.returncode != 0:
        raise DevError("could not start tree-eclass-dev.service")
    wait_unit("tree-eclass-dev.service", True)
    wait_health("tree-eclass-dev")


def cmd_up(_: argparse.Namespace) -> int:
    root = checkout_root()
    rev = git_rev(root, allow_dirty=True)
    prod, dev = prod_state(), dev_state()
    if active("tree-eclass-dev.service"):
        print("Dev stack is already running; use `tree-dev sync` to rebuild it.")
        return 0
    image_rev = dev_image_rev()
    if image_rev != rev:
        if image_rev is None:
            print("No :dev image yet; building from the working checkout (dirty allowed).")
        else:
            print(f":dev is rev {image_rev[:12]} but the checkout is {rev[:12]}; rebuilding (dirty allowed).")
        build_image(root, "dev", rev)
    # Prod keeps serving throughout; staging gets its own route and branch.
    try:
        branch = branch_from_prod(prod, dev, rev)
        print("Migrating the dev branch ...")
        systemctl("stop", "tree-eclass-dev-migrate.service")
        run_oneshot("tree-eclass-dev-migrate.service")
        print("Starting the dev stack ...")
        start_dev_stack()
        clear_tree_route()
    except BaseException:
        systemctl("stop", "tree-eclass-dev.service")
        wipe_branch(dev)
        raise
    print(f"Staging is live on branched data {branch} (rev {rev[:12]}).")
    print("Prod keeps serving; `tree-dev down` discards the branch.")
    return 0


def cmd_sync(_: argparse.Namespace) -> int:
    root = checkout_root()
    rev = git_rev(root, allow_dirty=True)
    dev = dev_state()
    prod = prod_state()
    was_running = stop_dev_stack()
    if image_exists("dev"):
        # Keep the serving image as fallback until the new branch proves healthy.
        retagged = podman("tag", "localhost/tree-eclass:dev", "localhost/tree-eclass:dev-prev", timeout=120)
        if retagged.returncode:
            raise DevError(f"could not retain the previous :dev image: {retagged.stderr[-1000:]}")
    build_image(root, "dev", rev)
    wipe_branch(dev)
    try:
        branch = branch_from_prod(prod, dev, rev)
        print("Migrating the new branch ...")
        run_oneshot("tree-eclass-dev-migrate.service")
        print("Starting the dev stack on the new branch ...")
        start_dev_stack()
        clear_tree_route()
    except BaseException as failure:
        # The new branch never serves: discard it and restore the previous
        # code on a fresh prod branch, so staging keeps serving working code.
        print(f"New branch failed ({failure}); restoring the previous branch ...", file=sys.stderr)
        stop_dev_stack()
        restored = podman("tag", "localhost/tree-eclass:dev-prev", "localhost/tree-eclass:dev", timeout=120)
        if restored.returncode:
            raise DevError(
                f"staging migration failed ({failure}); and the previous :dev image "
                f"could not be restored: {restored.stderr[-1000:]}"
            ) from failure
        wipe_branch(dev)
        previous_rev = git_rev(root, allow_dirty=True)
        branch_from_prod(prod, dev, previous_rev)
        print("Migrating the restored branch ...")
        systemctl("stop", "tree-eclass-dev-migrate.service")
        run_oneshot("tree-eclass-dev-migrate.service")
        start_dev_stack()
        raise DevError(
            f"staging now serves the previous code on fresh prod data; the new branch was discarded: {failure}"
        ) from failure
    if not was_running:
        print("Note: dev stack was not running before; it is now up on staging.")
    print(f"Staging rebuilt from {rev[:12]} on branch {branch} (frontend shipped inside the image).")
    return 0


def cmd_down(_: argparse.Namespace) -> int:
    dev = dev_state()
    if not stop_dev_stack():
        print("Dev stack is not running.")
    branch_file = dev / "branch"
    branch = branch_file.read_text().strip() if branch_file.is_file() else "unknown"
    clear_tree_route()
    wipe_branch(dev)
    print(f"Discarded dev branch {branch}.")
    print("Staging answers 502 until the next `up`; production is untouched.")
    return 0


def cmd_stage(_: argparse.Namespace) -> int:
    root = checkout_root()
    rev = git_rev(root, allow_dirty=False)
    with tempfile.TemporaryDirectory(prefix="tree-stage-") as temporary:
        image_build.authorize_bases(build_target(), root, require_aged=False, directory=Path(temporary))
    print(f"Warmed upstream bases for {rev[:12]}; deploy once all are seven-day eligible.")
    return 0


def checked_production_image(image: str) -> str:
    return image_build.checked_image("tree-eclass", image)


def retain_previous_image(record: dict) -> str:
    """Pin only the journal's exact image, including recovery before the pin."""
    retained = f"localhost/tree-eclass:checkpoint-{record['id']}"
    exists = podman("image", "exists", retained, role="tree-eclass", timeout=30)
    if exists.returncode not in (0, 1):
        raise DevError("could not inspect the retained recovery image")
    if exists.returncode == 1:
        original = checked_production_image(record["image"])
        if original.removeprefix("sha256:") != record["image"].removeprefix("sha256:"):
            raise DevError("pre-deployment image identity changed")
        pinned = podman("tag", record["image"], retained, role="tree-eclass", timeout=30)
        if pinned.returncode:
            raise DevError(f"could not retain the pre-deployment image: {pinned.stderr[-1000:]}")
    if checked_production_image(retained).removeprefix("sha256:") != record["image"].removeprefix("sha256:"):
        raise DevError("retained recovery tag does not match the journal's original image")
    return retained


def set_stable(image: str) -> None:
    image_build.set_tag("tree-eclass", image, "localhost/tree-eclass:stable")


def remove_production_container() -> None:
    image_build.remove_container("tree-eclass", "tree-eclass")


def restore_release(store: RecoveryStore, record: dict) -> None:
    state = prod_state()
    if Path(record["state"]) != state:
        store.block(record)
        raise DevError(
            "pending release state does not match the declared production state; refusing recovery. "
            "Correct the declared state path, then run `tree-dev recover`."
        )
    try:
        store.block(record)
        stop_production_writers(state)
        retained = retain_previous_image(record)
        rescue = store.restore(record)
        set_stable(retained)
        remove_production_container()
        store.admit(record)
        if record["was_running"]:
            print("Restored previous code and data; restarting production ...", file=sys.stderr)
            start_prod()
            wait_health("tree-eclass", timeout=180)
        store.complete(record, "restored")
        if rescue is not None:
            print(f"Failed release data retained privately at {rescue}.", file=sys.stderr)
    except BaseException as error:
        failures = [str(error) or type(error).__name__]
        # Even a failed old-runtime health check must not leave writers admitted.
        try:
            store.block(record)
        except BaseException as blocked:
            failures.append(f"could not ensure startup block: {blocked}")
        try:
            stop_production_writers(state)
        except BaseException as stopped:
            failures.append(f"could not prove all writers stopped: {stopped}")
        raise DevError(
            f"recovery failed: {'; '.join(failures)}. Release remains unresolved; "
            "fix the reported cause and run `tree-dev recover` before any other mutation."
        ) from error


def cmd_recover(args: argparse.Namespace) -> int:
    store = getattr(args, "recovery", None) or recovery_store()
    record = store.pending()
    if record is None:
        print("No interrupted Tree release to recover.")
        return 0
    if record.get("admitted") and record.get("phase") != "restored":
        print(
            "Warning: recovering the previous release discards writes made since its checkpoint; "
            "the interrupted dataset will be retained privately for rescue.",
            file=sys.stderr,
        )
    restore_release(store, record)
    print("Previous Tree release recovered.")
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    store = getattr(args, "recovery", None) or recovery_store()
    root = checkout_root()
    rev = git_rev(root, allow_dirty=False)
    short = rev[:12]
    prod = prod_state()
    # The staging stack may keep running: deploy promotes committed code to
    # prod state only and never reads the dev branch.
    target = build_target()
    with tempfile.TemporaryDirectory(prefix="tree-deploy-") as temporary:
        specification, rendered = image_build.authorize_bases(
            target, root, require_aged=True, directory=Path(temporary)
        )
        identity = image_build.candidate_image(target, root, rev, specification, rendered)
        image_build.transfer_image(target, identity)
    previous_id = checked_production_image("localhost/tree-eclass:stable")
    was_running = production_running()
    unknown = [
        container["Name"]
        for container in production_containers()
        if container["Name"].lstrip("/") not in PRODUCTION_CONTAINERS and writing_container(container, prod.resolve())
    ]
    if unknown:
        raise DevError(
            f"externally managed production writers are running: {', '.join(unknown)}; "
            "stop those writers before deployment (they were not changed)"
        )
    record = store.begin(prod, previous_id, identity, was_running)
    try:
        store.block(record)
        stop_production_writers(prod)
        print("Checkpointing cold production data ...")
        store.checkpoint(record)
        retain_previous_image(record)
        print("Migrating production data ...")
        # The manual-only Nix unit uses a root-controlled immutable-image permit.
        migrate_production(store, record)
        # Quadlet resolves :stable at unit start; a cached container would
        # keep serving the previous image. Remove it so the next start
        # recreates the container from the new :stable.
        set_stable(identity)
        remove_production_container()
        store.admit(record)
        print(f"Promoted {short} to :stable; restarting production ...")
        start_prod()
        wait_health("tree-eclass", timeout=180)
        store.complete(record, "activated")
    except BaseException as failure:
        print("Deployment failed; recovering previous code and data ...", file=sys.stderr)
        try:
            restore_release(store, record)
        except BaseException as recovery:
            raise DevError(f"deployment failed ({str(failure) or type(failure).__name__}); {recovery}") from failure
        print("Deployment failed; previous code and data restored.", file=sys.stderr)
        raise
    print(f"Production is live on {short}.")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    pending = recovery_store().pending()
    if pending is not None:
        print(f"release: pending {pending['id']} ({pending.get('phase', 'unknown')}); run `tree-dev recover`")
    prod_running = active("tree-eclass.service")
    dev_running = active("tree-eclass-dev.service")
    dev = dev_state()
    branch_file = dev / "branch"
    branch = branch_file.read_text().strip() if branch_file.is_file() else None
    rev_file = dev / "rev"
    rev = rev_file.read_text().strip() if rev_file.is_file() else None
    stable = podman(
        "image",
        "inspect",
        "--format",
        '{{ index .Labels "org.opencontainers.image.revision" }}',
        "localhost/tree-eclass:stable",
        timeout=30,
        role="tree-eclass",
    )
    stable_rev = stable.stdout.strip() if stable.returncode == 0 else "(no :stable image)"
    print(
        f"prod: {'running' if prod_running else 'stopped'} (:stable rev {stable_rev[:12] if len(stable_rev) >= 12 else stable_rev})"
    )
    if dev_running:
        staging_host = None
        try:
            rendered = json.loads((dev / "jobs" / "application.json").read_text())
            hosts = rendered.get("allowed_hosts", [])
            candidates = [host for host in hosts if host not in ("127.0.0.1", "localhost")]
            staging_host = next((host for host in candidates if "dev" in host), candidates[0] if candidates else None)
        except (OSError, ValueError):
            staging_host = None
        where = f"https://{staging_host} " if staging_host else ""
        print(f"staging: running ({where}branch {branch}, rev {(rev[:12] if rev else '?')})")
    else:
        print("staging: down (answers 502; `tree-dev up` to stage)")
    for name in (("tree-eclass-dev", "tree-eclass") if dev_running else ("tree-eclass",)):
        if container_health(name):
            print(f"health: {name} ok")
        else:
            print(f"health: {name} unreachable")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tree-dev", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up", help="Branch prod data and start the dev stack").set_defaults(run=cmd_up)
    sub.add_parser("sync", help="Rebuild :dev from the working checkout and restart").set_defaults(run=cmd_sync)
    sub.add_parser("down", help="Discard the dev branch and restore prod").set_defaults(run=cmd_down)
    sub.add_parser("stage", help="Warm declared upstream bases for seven-day observation").set_defaults(run=cmd_stage)
    sub.add_parser("deploy", help="Build clean HEAD on aged immutable bases and promote to production").set_defaults(
        run=cmd_deploy
    )
    sub.add_parser("recover", help="Restore previous code and data after an interrupted release").set_defaults(
        run=cmd_recover
    )
    sub.add_parser("status", help="Show prod/dev stack state").set_defaults(run=cmd_status)
    args = parser.parse_args(argv)
    try:
        require_admin()
        if os.getuid() != 0:
            return subprocess.run(
                [
                    tool("sudo"),
                    "-H",
                    "-u",
                    "root",
                    "--",
                    tool("env"),
                    "-i",
                    "HOME=/root",
                    "USER=root",
                    "LOGNAME=root",
                    "PATH=/run/current-system/sw/bin",
                    tool("tree_dev"),
                    *(sys.argv[1:] if argv is None else argv),
                ],
                cwd="/",
            ).returncode
        os.chdir("/")
        os.umask(0o077)
        if args.command == "status":
            return args.run(args)
        args.recovery = recovery_store()
        with args.recovery.lock():
            if args.command != "recover" and args.recovery.pending() is not None:
                raise DevError("an interrupted Tree release is unresolved; run `tree-dev recover` first")

            def interrupted(_number, _frame):
                raise KeyboardInterrupt

            previous_handler = signal.signal(signal.SIGTERM, interrupted)
            try:
                return args.run(args)
            finally:
                signal.signal(signal.SIGTERM, previous_handler)
    except KeyboardInterrupt:
        print("tree-dev interrupted.", file=sys.stderr)
        return 130
    except (
        PrincipalError,
        RecoveryError,
        DevError,
        OSError,
        ValueError,
        KeyError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        print(f"tree-dev: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
