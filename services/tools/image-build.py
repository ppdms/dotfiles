#!/usr/bin/env python3
"""Build, deploy and roll back self-built homelab service images.

Images are declared in the Nix-written build spec (`HOMELAB_BUILD_SPEC`). Each
is built from the clean committed HEAD of its git checkout by the shared
`image_build` pipeline: upstream FROM images are authorized through
`homelab-image-update inputs` (seven-day quarantine), the build is hermetic
(`--pull=never`, pinned Dockerfile rendering) and the resulting image ID is
streamed into the owning service's rootless Podman store. `stage` only warms
the base-image observation clock. `deploy` promotes the new image by retagging
`<repository>:stable` (the previous image stays reachable as `:previous`) and,
for served images, restarts the unit and waits for the container to be healthy.
A failed activation restores the previous image; an interrupted one stays
journaled and blocks every other mutation until `recover`. `rollback` swaps
`:previous` and `:stable`. These images never enter the registry pin feed.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

import image_build
from image_build import BuildError, BuildTarget, REVISION_LABEL, git_rev
from principals import PrincipalError, principal, principal_for_unit, require_admin, run_as, tool

LABEL_NAMESPACE = "io.homelab.image"
SPEC_PREFIXES = ("/nix/store/",)
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}\Z")
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}\Z")
BUILD_ARG = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
IMAGE_ID = re.compile(r"(sha256:)?[0-9a-f]{64}\Z")
PHASES = ("begun", "switching")
HEALTH_TIMEOUT = 180
RUNNING_PROOF = 15
# A from-source image (SilverBullet's Rust compile alone is ~15 minutes) must not
# be killed by the default build limit.
BUILD_TIMEOUT = 3600
LIVE_STATES = ("active", "reloading", "activating")


@dataclass(frozen=True)
class Image:
    target: BuildTarget
    unit: str
    container: str
    serve: bool

    @property
    def name(self) -> str:
        return self.target.name

    def reference(self, tag: str) -> str:
        return self.target.reference(tag)


@dataclass(frozen=True)
class Spec:
    recovery: Path
    images: dict[str, Image]


def parse_spec(value: object) -> Spec:
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise BuildError("build spec has an unsupported schema")
    builder, recovery, declared = value.get("builder"), value.get("recovery"), value.get("images")
    if not isinstance(builder, str) or not builder:
        raise BuildError("build spec names no builder principal")
    if not isinstance(recovery, str) or not Path(recovery).is_absolute() or ".." in Path(recovery).parts:
        raise BuildError("build spec recovery must be an absolute path")
    if not isinstance(declared, dict):
        raise BuildError("build spec images must be an object")
    images: dict[str, Image] = {}
    for name, entry in declared.items():
        if not isinstance(name, str) or not NAME.match(name) or not isinstance(entry, dict):
            raise BuildError(f"build spec has an invalid image name: {name!r}")

        def text(key: str) -> str:
            field = entry.get(key)
            if not isinstance(field, str) or not field:
                raise BuildError(f"build spec image {name} has no {key}")
            return field

        repository, checkout, dockerfile = text("repository"), text("checkout"), text("dockerfile")
        owner, unit, container = text("principal"), text("unit"), text("container")
        if repository != f"localhost/{name}":
            raise BuildError(f"build spec image {name} must use repository localhost/{name}")
        if not Path(checkout).is_absolute() or ".." in Path(checkout).parts:
            raise BuildError(f"build spec image {name} checkout must be an absolute path")
        if Path(dockerfile).is_absolute() or ".." in Path(dockerfile).parts:
            raise BuildError(f"build spec image {name} dockerfile must be relative to the checkout")
        if not unit.endswith(".service") or "/" in unit:
            raise BuildError(f"build spec image {name} unit must be a .service unit")
        if not CONTAINER.match(container):
            raise BuildError(f"build spec image {name} has an invalid container name")
        if not isinstance(entry.get("serve"), bool):
            raise BuildError(f"build spec image {name} serve must be a boolean")
        argument = entry.get("revisionBuildArg")
        if argument is not None and (not isinstance(argument, str) or not BUILD_ARG.match(argument)):
            raise BuildError(f"build spec image {name} has an invalid revisionBuildArg")
        images[name] = Image(
            target=BuildTarget(
                name=name,
                repository=repository,
                checkout=Path(checkout),
                dockerfile=dockerfile,
                builder=builder,
                owner=owner,
                label_namespace=LABEL_NAMESPACE,
                markers=(dockerfile,),
                revision_build_arg=argument,
                build_timeout=BUILD_TIMEOUT,
            ),
            unit=unit,
            container=container,
            serve=entry["serve"],
        )
    return Spec(recovery=Path(recovery), images=images)


def load_spec() -> Spec:
    configured = os.environ.get("HOMELAB_BUILD_SPEC")
    if not configured:
        raise BuildError("HOMELAB_BUILD_SPEC is not set")
    try:
        resolved = Path(configured).resolve(strict=True)
    except OSError as error:
        raise BuildError(f"build spec is unavailable: {error}") from error
    if not any(str(resolved).startswith(prefix) for prefix in SPEC_PREFIXES):
        raise BuildError("build spec must be installed in the Nix store")
    metadata = resolved.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in (0, os.geteuid())
        or metadata.st_mode & 0o022
    ):
        raise BuildError("build spec must be a root-owned, non-writable regular file")
    try:
        return parse_spec(json.loads(resolved.read_text()))
    except ValueError as error:
        raise BuildError(f"build spec is not valid JSON: {error}") from error


def select(spec: Spec, name: str) -> Image:
    try:
        image = spec.images[name]
    except KeyError:
        raise BuildError(
            f"unknown image {name!r}; declared images: {', '.join(sorted(spec.images)) or '(none)'}"
        ) from None
    if principal_for_unit(image.unit) != image.target.owner:
        raise BuildError(f"{image.unit} is not a unit of principal {image.target.owner}")
    principal(image.target.builder)
    principal(image.target.owner)
    return image


class Journal:
    """Root-owned per-image record of an activation that has not yet settled."""

    def __init__(self, root: Path, name: str):
        self.root = root
        self.directory = root / name
        self.name = name

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise BuildError(f"unsafe image build journal root: {self.root}")
        try:
            self.directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BuildError(f"unsafe image build journal directory: {self.directory}")

    def _read(self, path: Path) -> dict | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise BuildError(f"cannot read image build journal {path}: {error}") from error
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise BuildError(f"unsafe image build journal record: {path}")
            content = stream.read(65537)
        if len(content) > 65536:
            raise BuildError(f"oversized image build journal record: {path}")
        try:
            value = json.loads(content)
        except (ValueError, UnicodeError) as error:
            raise BuildError(f"malformed image build journal record: {path}") from error
        if not isinstance(value, dict):
            raise BuildError(f"malformed image build journal record: {path}")
        return value

    def _write(self, path: Path, value: dict) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".journal-", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def lock(self):
        self._ensure()
        descriptor = os.open(self.directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise BuildError("unsafe image build lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BuildError(f"another operation holds the {self.name} image build lock") from None
            yield self
        finally:
            os.close(descriptor)

    def pending(self) -> dict | None:
        if not self.directory.exists():
            return None
        record = self._read(self.directory / "pending.json")
        if record is None:
            return None
        valid = (
            isinstance(record.get("id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", record["id"])
            and record.get("name") == self.name
            and record.get("operation") in ("deploy", "rollback")
            and isinstance(record.get("rev"), str)
            and isinstance(record.get("candidate"), str)
            and IMAGE_ID.match(record["candidate"])
            and all(record.get(key) is None or IMAGE_ID.match(str(record.get(key))) for key in ("previous", "older"))
            and isinstance(record.get("was_running"), bool)
            and isinstance(record.get("serve"), bool)
            and record.get("phase") in PHASES
        )
        if not valid:
            raise BuildError(f"invalid pending image build journal for {self.name}")
        return record

    def last(self) -> dict | None:
        return self._read(self.directory / "last.json") if self.directory.exists() else None

    def begin(self, record: dict) -> None:
        self._ensure()
        self._write(self.directory / "pending.json", record)

    def advance(self, record: dict, phase: str) -> None:
        record["phase"] = phase
        self._write(self.directory / "pending.json", record)

    def complete(self, record: dict) -> None:
        self._write(
            self.directory / "last.json",
            {
                "id": record["id"],
                "operation": record["operation"],
                "rev": record["rev"],
                "image": record["candidate"],
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "phase": "activated",
            },
        )
        self.settle()

    def settle(self) -> None:
        (self.directory / "pending.json").unlink(missing_ok=True)
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def systemctl(image: Image, *arguments: str, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return run_as(
            image.target.owner,
            [tool("systemctl"), "--user", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except OSError as error:
        raise BuildError(f"could not run systemctl --user: {error}") from error


def unit_state(image: Image) -> str:
    result = systemctl(image, "show", "--property=ActiveState", "--value", image.unit)
    if result.returncode:
        raise BuildError(f"could not determine the state of {image.unit}: {result.stderr.strip()[-500:]}")
    return result.stdout.strip()


def unit_active(image: Image) -> bool:
    return unit_state(image) in LIVE_STATES


def wait_unit(image: Image, want_active: bool, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if unit_active(image) == want_active:
            return
        time.sleep(2)
    raise BuildError(f"{image.unit} did not become {'active' if want_active else 'inactive'} within {timeout}s")


def stop_unit(image: Image) -> None:
    if not unit_active(image):
        return
    stopped = systemctl(image, "stop", image.unit)
    if stopped.returncode:
        raise BuildError(f"could not stop {image.unit}: {stopped.stderr.strip()[-1000:]}")
    wait_unit(image, False)


def start_unit(image: Image) -> None:
    systemctl(image, "reset-failed", image.unit)  # a failed unit refuses to start; nothing to clear otherwise
    started = systemctl(image, "start", image.unit)
    if started.returncode:
        raise BuildError(f"could not start {image.unit}: {started.stderr.strip()[-1000:]}")
    wait_unit(image, True)
    wait_healthy(image)


def container_state(image: Image) -> tuple[str, bool]:
    """Health status ("" without a healthcheck) and running flag of the service container."""
    result = image_build.podman(
        image.target.owner,
        "inspect",
        "--format",
        "{{with .State.Health}}{{.Status}}{{end}}|{{.State.Running}}",
        image.container,
        timeout=30,
    )
    if result.returncode:
        return "", False
    status, _, running = result.stdout.strip().partition("|")
    return status, running == "true"


def wait_healthy(image: Image, timeout: int = HEALTH_TIMEOUT, running_proof: int = RUNNING_PROOF) -> None:
    deadline = time.monotonic() + timeout
    running_since: float | None = None
    while True:
        now = time.monotonic()
        status, running = container_state(image)
        if status == "healthy":
            return
        if status == "" and running:
            running_since = now if running_since is None else running_since
            if now - running_since >= running_proof:
                return
        else:
            running_since = None
        if now >= deadline:
            raise BuildError(
                f"container {image.container} did not become healthy within {timeout}s "
                f"(health {status or 'none'}, running {running})"
            )
        time.sleep(2)


def owner_image(image: Image, tag: str) -> str | None:
    """Image ID behind `<repository>:<tag>` in the owner store, or None when absent."""
    reference = image.reference(tag)
    if not image_build.image_exists(image.target.owner, reference):
        return None
    return image_build.checked_image(image.target.owner, reference)


def same_image(left: str, right: str) -> bool:
    return left.removeprefix("sha256:") == right.removeprefix("sha256:")


def untag(image: Image, tag: str) -> None:
    reference = image.reference(tag)
    result = image_build.podman(image.target.owner, "untag", reference, reference, timeout=30)
    if result.returncode:
        raise BuildError(f"could not remove {reference}: {result.stderr[-1000:]}")


def activate(image: Image, journal: Journal, record: dict) -> None:
    owner = image.target.owner
    previous = record["previous"]
    if image.serve:
        print(f"Stopping {image.unit} ...")
        stop_unit(image)
    journal.advance(record, "switching")
    if previous is not None:
        image_build.set_tag(owner, previous, image.reference("previous"))
        retained = image_build.checked_image(owner, image.reference("previous"))
        if not same_image(retained, previous):
            raise BuildError(f"{image.reference('previous')} does not match the journaled previous image")
    image_build.set_tag(owner, record["candidate"], image.reference("stable"))
    if image.serve:
        # Quadlet resolves :stable at container creation; drop a cached container.
        image_build.remove_container(owner, image.container)
        print(f"Promoted {record['rev'][:12] or record['candidate'][:19]} to :stable; starting {image.unit} ...")
        start_unit(image)


def restore(image: Image, journal: Journal, record: dict) -> None:
    """Put the pre-operation image back; idempotent for any interruption point."""
    owner = image.target.owner
    previous, older = record["previous"], record["older"]
    try:
        if image.serve:
            stop_unit(image)
        if previous is not None:
            image_build.checked_image(owner, previous)
            image_build.set_tag(owner, previous, image.reference("stable"))
            if older is not None:
                try:
                    image_build.set_tag(owner, older, image.reference("previous"))
                except BuildError as error:
                    print(f"Warning: could not reset :previous: {error}", file=sys.stderr)
            elif owner_image(image, "previous") is not None:
                untag(image, "previous")
        else:
            current = owner_image(image, "stable")
            if current is not None and same_image(current, record["candidate"]):
                untag(image, "stable")
        if image.serve:
            image_build.remove_container(owner, image.container)
            if previous is not None and record["was_running"]:
                print(f"Restarting {image.unit} on the previous image ...", file=sys.stderr)
                start_unit(image)
    except BaseException as error:
        raise BuildError(
            f"restoring {image.name} failed: {str(error) or type(error).__name__}. "
            f"Run `homelab-image-build recover {image.name}` once the cause is fixed."
        ) from error
    journal.settle()
    if previous is None:
        print(
            f"No previous {image.name} image existed: :stable was removed and {image.unit} is left stopped.",
            file=sys.stderr,
        )


def run_operation(image: Image, journal: Journal, operation: str, rev: str, candidate: str) -> None:
    """Journal, activate and, on any failure, restore and re-raise the original error."""
    owner = image.target.owner
    previous = owner_image(image, "stable")
    older = owner_image(image, "previous")
    if operation == "rollback" and (previous is None or older is None):
        raise BuildError(f"rollback needs both :stable and :previous in the {owner} store")
    record = {
        "id": uuid.uuid4().hex,
        "name": image.name,
        "operation": operation,
        "rev": rev,
        "previous": previous,
        "older": older,
        "candidate": candidate,
        "was_running": unit_active(image) if image.serve else False,
        "serve": image.serve,
        "phase": "begun",
    }
    journal.begin(record)
    try:
        activate(image, journal, record)
        journal.complete(record)
    except BaseException as failure:
        print(f"{operation} of {image.name} failed; restoring the previous image ...", file=sys.stderr)
        try:
            restore(image, journal, record)
        except BaseException as recovery:
            raise BuildError(
                f"{operation} failed ({str(failure) or type(failure).__name__}); {recovery}"
            ) from failure
        print(f"{operation} failed; previous {image.name} image restored.", file=sys.stderr)
        raise
    if image.serve:
        print(f"{image.name} is live on {rev[:12] or candidate[:19]}.")
    else:
        print(f"{image.name} {rev[:12] or candidate[:19]} is staged as {image.reference('stable')}; {image.unit} was not touched.")


def cmd_list(context: argparse.Namespace) -> int:
    for name, image in sorted(context.spec.images.items()):
        mode = "serve" if image.serve else "stage-only"
        print(f"{name}\t{image.reference('stable')}\t{image.unit}\t{mode}")
    return 0


def cmd_stage(context: argparse.Namespace) -> int:
    image = select(context.spec, context.name)
    root = image_build.checkout_root(image.target)
    rev = git_rev(root, allow_dirty=False)
    with tempfile.TemporaryDirectory(prefix="image-stage-") as temporary:
        image_build.authorize_bases(image.target, root, require_aged=False, directory=Path(temporary))
    print(f"Warmed upstream bases for {image.name} {rev[:12]}; deploy once all are seven-day eligible.")
    return 0


def cmd_deploy(context: argparse.Namespace) -> int:
    image = select(context.spec, context.name)
    target = image.target
    root = image_build.checkout_root(target)
    rev = git_rev(root, allow_dirty=False)
    with tempfile.TemporaryDirectory(prefix="image-deploy-") as temporary:
        specification, rendered = image_build.authorize_bases(
            target, root, require_aged=True, directory=Path(temporary)
        )
        candidate = image_build.candidate_image(target, root, rev, specification, rendered)
        image_build.transfer_image(target, candidate)
    run_operation(image, context.journal, "deploy", rev, candidate)
    return 0


def cmd_rollback(context: argparse.Namespace) -> int:
    image = select(context.spec, context.name)
    older = owner_image(image, "previous")
    if older is None:
        raise BuildError(f"{image.reference('previous')} does not exist; nothing to roll back to")
    labels = image_build.podman(
        image.target.owner, "image", "inspect", "--format", f'{{{{ index .Labels "{REVISION_LABEL}" }}}}', older, timeout=30
    )
    rev = labels.stdout.strip() if labels.returncode == 0 else ""
    run_operation(image, context.journal, "rollback", rev, older)
    return 0


def cmd_recover(context: argparse.Namespace) -> int:
    image = select(context.spec, context.name)
    record = context.journal.pending()
    if record is None:
        print(f"No interrupted {image.name} operation to recover.")
        return 0
    restore(image, context.journal, record)
    print(f"Previous {image.name} image recovered.")
    return 0


def describe(image: Image, tag: str) -> str:
    reference = image.reference(tag)
    owner = image.target.owner
    if not image_build.image_exists(owner, reference):
        return f"(no :{tag} image)"
    labels = image_build.inspect_labels(owner, reference) or {}
    identity = image_build.checked_image(owner, reference)
    return f"rev {str(labels.get(REVISION_LABEL, '?'))[:12]} ({identity.removeprefix('sha256:')[:12]})"


def cmd_status(context: argparse.Namespace) -> int:
    names = [context.name] if context.name else sorted(context.spec.images)
    for name in names:
        image = select(context.spec, name)
        journal = Journal(context.spec.recovery, name)
        print(f"{name}: {'served' if image.serve else 'image-only'} ({image.unit})")
        print(f"  stable:   {describe(image, 'stable')}")
        print(f"  previous: {describe(image, 'previous')}")
        last = journal.last()
        if last is None:
            print("  last:     (never deployed by homelab-image-build)")
        else:
            print(f"  last:     {last.get('operation')} {str(last.get('rev', ''))[:12]} at {last.get('time')} ({last.get('phase')})")
        pending = journal.pending()
        if pending is not None:
            print(
                f"  pending:  {pending['operation']} {pending['id']} ({pending['phase']}); "
                f"run `homelab-image-build recover {name}`"
            )
        if image.serve:
            print(f"  unit:     {unit_state(image)}")
            status, running = container_state(image)
            print(f"  health:   {status or ('running, no healthcheck' if running else 'not running')}")
        else:
            print("  unit:     not managed (serve is false)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="homelab-image-build", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List declared self-built images").set_defaults(run=cmd_list)
    for command, run, text in (
        ("stage", cmd_stage, "Warm upstream bases for seven-day observation; builds nothing"),
        ("deploy", cmd_deploy, "Build clean HEAD on aged bases and promote it to :stable"),
        ("rollback", cmd_rollback, "Swap :stable and :previous"),
        ("recover", cmd_recover, "Restore the previous image after an interrupted operation"),
    ):
        sub.add_parser(command, help=text).set_defaults(run=run)
        sub.choices[command].add_argument("name", help="declared image name")
    status = sub.add_parser("status", help="Show image, journal and service state")
    status.add_argument("name", nargs="?", help="declared image name (default: all)")
    status.set_defaults(run=cmd_status)
    return parser


READ_ONLY = ("list", "status")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
                    tool("image_build"),
                    *(sys.argv[1:] if argv is None else argv),
                ],
                cwd="/",
            ).returncode
        os.chdir("/")
        os.umask(0o077)
        args.spec = load_spec()
        if args.command in READ_ONLY:
            return args.run(args)
        select(args.spec, args.name)
        args.journal = Journal(args.spec.recovery, args.name)

        def interrupted(_number, _frame):
            raise KeyboardInterrupt

        with args.journal.lock():
            if args.command != "recover" and args.journal.pending() is not None:
                raise BuildError(
                    f"an interrupted {args.name} operation is unresolved; run `homelab-image-build recover {args.name}` first"
                )
            previous_handler = signal.signal(signal.SIGTERM, interrupted)
            try:
                return args.run(args)
            finally:
                signal.signal(signal.SIGTERM, previous_handler)
    except KeyboardInterrupt:
        print("homelab-image-build interrupted.", file=sys.stderr)
        return 130
    except (PrincipalError, BuildError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"homelab-image-build: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
