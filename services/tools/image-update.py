#!/usr/bin/env python3
"""Observe immutable images and publish seven-day-aged pins for native flake updates."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

from image_age import AgeError, AgeStore, atomic_json
from image_registry import Registry, RegistryError, stable_tags
from tree_image_inputs import collect_inputs, render_dockerfile
from principals import (PrincipalError, current_principal, manifest, principal,
                        principal_for_unit, registry_archive_path, require_admin,
                        run_as, stream_image, tool)


class UpdateError(RuntimeError):
    pass


def command(*args: str, timeout: int = 600, **kwargs) -> str:
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise UpdateError(f"{args[0]} failed: {result.stderr.strip()[-1200:]}")
    return result.stdout.strip()


def git(feed: Path, *args: str) -> str:
    return command(tool("git"), "-c", "commit.gpgsign=false", "-c", "user.name=Homelab image updater",
                   "-c", "user.email=homelab-image-updater@localhost", "-C", str(feed), *args)


def validate_pins(pins: dict, policy: dict) -> None:
    if pins.get("schema") != 1 or set(pins.get("images", {})) != set(policy["images"]):
        raise UpdateError("image feed and policy keys differ; refusing an incomplete pin set")
    for key, pin in pins["images"].items():
        repository = policy["images"][key]["repository"]
        reference = pin["reference"]
        if not re.fullmatch(re.escape(repository) + r"(?::[^@\s]+)?@sha256:[0-9a-f]{64}", reference):
            raise UpdateError(f"{key}: pin is not an immutable reference to its configured repository")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", pin["version"]):
            raise UpdateError(f"{key}: pin has no numeric stable version")


def read_policy(path: Path) -> dict:
    policy = json.loads(path.read_text())
    if policy.get("schema") != 1 or not policy.get("images"):
        raise UpdateError("unsupported or empty image update policy")
    return policy


@contextmanager
def locked(state: Path, *, wait: bool = False):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise UpdateError("another image observation/update is running") from None
        yield


def initialize(feed: Path, seed: Path, policy: dict) -> None:
    if (feed / ".git").exists():
        validate_pins(json.loads((feed / "pins.json").read_text()), policy)
        if git(feed, "status", "--porcelain"):
            raise UpdateError("managed image feed has uncommitted changes; refusing to overwrite them")
        return
    if feed.exists() and any(feed.iterdir()):
        raise UpdateError("image feed destination is not an empty directory or managed Git repository")
    pins = json.loads(seed.read_text())
    validate_pins(pins, policy)
    feed.mkdir(parents=True, exist_ok=True, mode=0o700)
    git(feed, "init", "--initial-branch=main")
    atomic_json(feed / "pins.json", pins)
    git(feed, "add", "pins.json")
    git(feed, "commit", "-m", "Initialize managed image feed from declared pins")
    print(f"Initialized {feed} from declared pins; image updates require seven-day age evidence.")


def publish(feed: Path, pins: dict, policy: dict) -> bool:
    validate_pins(pins, policy)
    path = feed / "pins.json"
    original = path.read_bytes()
    if json.loads(original) == pins:
        return False
    if git(feed, "status", "--porcelain"):
        raise UpdateError("managed image feed has uncommitted changes; refusing to overwrite them")
    try:
        atomic_json(path, pins)
        git(feed, "add", "pins.json")
        git(feed, "commit", "-m", "Publish seven-day-aged service image pins")
    except BaseException:
        path.write_bytes(original)
        git(feed, "reset", "--", "pins.json")
        raise
    return True


def version_order(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def choose_pins(policy: dict, current: dict, store: AgeStore, available: dict) -> dict:
    # Each unit is either one application or a lockstep release family.
    units: dict[str, list[str]] = {}
    for key, spec in policy["images"].items():
        units.setdefault(spec.get("group", key), []).append(key)
    result = json.loads(json.dumps(current))
    for keys in units.values():
        if any(key not in available for key in keys):
            continue
        choices = {}
        for key in keys:
            candidates = store.data["candidates"].get(key, {}).values()
            choices[key] = {}
            for candidate in candidates:
                if candidate["tag"] not in available[key] or not store.eligible(candidate["artifact"]):
                    continue
                floor = current["images"][key]
                if floor.get("unreleased_baseline") and version_order(candidate["version"]) <= version_order(floor["version"]):
                    continue
                if version_order(candidate["version"]) < version_order(current["images"][key]["version"]):
                    continue
                previous = choices[key].get(candidate["version"])
                rank = (candidate["order"], candidate["first_seen"])
                if previous is None or rank > (previous["order"], previous["first_seen"]):
                    choices[key][candidate["version"]] = candidate
        common = set.intersection(*(set(choices[key]) for key in keys))
        if not common:
            continue
        version = max(common, key=version_order)
        for key in keys:
            chosen = choices[key][version]
            old = current["images"][key]
            # Same-version rebuilds must not move backwards in their build order.
            if version == old["version"] and chosen["order"] < old.get("order", []):
                break
        else:
            for key in keys:
                chosen = choices[key][version]
                result["images"][key] = {
                    "reference": chosen["artifact"]["reference"],
                    "version": version, "order": chosen["order"],
                }
    return result


def scan(feed: Path, seed: Path, policy: dict, state: Path, registry_factory=Registry) -> int:
    initialize(feed, seed, policy)
    store = AgeStore(state / "observations.json")
    current = json.loads((feed / "pins.json").read_text())
    registries = {}
    available = {}
    tags = {}
    errors = []
    for key, spec in policy["images"].items():
        try:
            registry = registry_factory()
            registries[key] = registry
            catalog = (registry.release_tags(spec["release_repository"])
                       if "release_repository" in spec else registry.tags(spec["repository"]))
            candidates = stable_tags(spec, catalog)
            if not candidates:
                raise RegistryError("no tags match the stable compatibility track")
            tags[key] = candidates
            available[key] = {candidate[1] for candidate in candidates}
        except (RegistryError, OSError, ValueError) as error:
            errors.append(f"{key}: {error}")
    groups = {}
    for key, spec in policy["images"].items():
        groups.setdefault(spec.get("group", key), []).append(key)

    def group_winner_version(keys: list[str]) -> str | None:
        proposed = choose_pins(policy, current, store, available)
        if all(proposed["images"][key] == current["images"][key] for key in keys):
            return None
        return proposed["images"][keys[0]]["version"]

    for keys in groups.values():
        if any(key not in tags for key in keys):
            continue
        versions = set.intersection(*(set(tag[2] for tag in tags[key]) for key in keys))
        if not versions:
            errors.append(f"{', '.join(keys)}: no common stable release")
            for key in keys:
                available.pop(key, None)
            continue
        inspected: set[tuple[str, str]] = set()
        group_done = False
        for version in sorted(versions, key=version_order, reverse=True):
            choices = {}
            for key in keys:
                floor = current["images"][key]
                choices[key] = [
                    candidate for candidate in tags[key]
                    if candidate[2] == version
                    and (version_order(version) > version_order(floor["version"])
                         or (version == floor["version"]
                             and not floor.get("unreleased_baseline")
                             and list(candidate[0]) >= floor.get("order", [])))
                ]
            if any(not choices[key] for key in keys):
                continue
            # Missing publication only quarantines that candidate under
            # honest observation; it never stops historical discovery.
            # A winner is trusted only after its version and all newer
            # versions were fully inspected in this scan, so fresh young
            # evidence always overrides stale stored eligibility.
            # Older versions below a fresh complete eligible group cannot
            # win, so the scan stops instead of crawling ancient history.
            for index in range(max(len(choices[key]) for key in keys)):
                for key in keys:
                    if index >= len(choices[key]):
                        continue
                    order, tag, _ = choices[key][index]
                    if (key, tag) in inspected:
                        continue
                    inspected.add((key, tag))
                    try:
                        artifact = registries[key].inspect(policy["images"][key]["repository"], tag)
                        store.observe(artifact)
                        records = store.data["candidates"].setdefault(key, {})
                        previous = records.get(artifact["manifest"])
                        records[artifact["manifest"]] = {
                            "tag": tag, "version": version, "order": list(order), "artifact": artifact,
                            "first_seen": previous["first_seen"] if previous else store.now,
                        }
                        deadline = store.eligible_at(artifact)
                        status = "eligible" if store.eligible(artifact) else "quarantined until " + datetime.fromtimestamp(deadline, timezone.utc).isoformat()
                        print(f"{key}: {tag} {artifact['manifest']} {status} ({store.evidence_source(artifact)})")
                    except (RegistryError, AgeError, OSError, ValueError) as error:
                        errors.append(f"{key}: {error}")
                        available.pop(key, None)
                        # Finish the remaining members of this index for
                        # observation, then retain the existing group without
                        # partial promotion.
                        group_done = True
                if group_done:
                    break
            if group_done:
                break
            winner = group_winner_version(keys)
            if winner is not None and version_order(winner) >= version_order(version):
                break
    proposed = choose_pins(policy, current, store, available)
    # Revalidate availability at promotion, using the immutable digest, never
    # a tag that may have changed while the image was in quarantine.
    for keys in groups.values():
        if all(proposed["images"][key] == current["images"][key] for key in keys):
            continue
        try:
            for key in keys:
                pin = proposed["images"][key]
                artifact = registries[key].inspect(policy["images"][key]["repository"], pin["reference"].split("@", 1)[1])
                store.observe(artifact)
                if artifact["reference"] != pin["reference"] or not store.eligible(artifact):
                    raise UpdateError("artifact changed or age evidence is incomplete")
        except (RegistryError, UpdateError, AgeError, OSError, ValueError) as error:
            errors.append(f"{', '.join(keys)}: promotion blocked: {error}")
            for key in keys:
                proposed["images"][key] = current["images"][key]
    store.save()
    if publish(feed, proposed, policy):
        print("Published approved pins. Run rebuild-update to select this private feed revision and rebuild.")
    else:
        print("No newly eligible image pins; existing feed unchanged.")
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


def image_present(role: str, image: str) -> bool:
    result = run_as(role, [tool("podman"), "image", "exists", image],
                    text=True, capture_output=True, timeout=60)
    if result.returncode not in (0, 1):
        raise UpdateError(f"{role}: cannot check image presence; runtime image store is unavailable")
    return result.returncode == 0


def validate_registry_archive(archive: Path, image: str) -> None:
    """Verify the exact reference and manifest; native target load verifies blobs."""
    if not stat.S_ISREG(archive.lstat().st_mode):
        raise UpdateError("registry image cache must contain only regular archives")
    source = f"oci-archive:{archive}:{image}"
    result = run_as("image-updates", [tool("skopeo"), "inspect", "--raw", source],
                    capture_output=True, timeout=300)
    if result.returncode:
        raise UpdateError(f"invalid registry image archive for {image}")
    if "sha256:" + hashlib.sha256(result.stdout).hexdigest() != image.split("@", 1)[1]:
        raise UpdateError(f"registry image archive does not retain the pinned digest for {image}")
    # Acquisition's Skopeo copy checks downloaded blobs. Inspect does not read
    # cached layer data: the destination's native Podman load must reject corrupt
    # blobs before distribution succeeds or any workload starts.


def acquire_images(images: list[str], signature_policy: Path) -> int:
    """Publish private exact-registry OCI archives, never lossy Podman exports."""
    if current_principal() != "image-updates":
        raise UpdateError("only the image-updates account may acquire registry images")
    registry = Registry()
    for image in images:
        archive = registry_archive_path(image)
        cache = archive.parent
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        if cache.is_symlink() or not cache.is_dir():
            raise UpdateError("registry image cache must be a private directory")
        cache.chmod(0o700)
        if archive.exists() or archive.is_symlink():
            validate_registry_archive(archive, image)
            archive.chmod(0o600)
            continue
        repository = image.split("@", 1)[0]
        credentials = registry.github_credentials() if repository.startswith("ghcr.io/") else None
        with tempfile.TemporaryDirectory(prefix=".acquire-", dir=cache) as staging_directory:
            staging = Path(staging_directory) / "image.tar"
            staging.touch(mode=0o600)
            arguments = ["--policy", str(signature_policy), "copy", "--all", "--preserve-digests"]
            transports = [f"docker://{image}", f"oci-archive:{staging}:{image}"]
            if credentials is None:
                role_command("image-updates", tool("skopeo"), *arguments, *transports, timeout=1800)
            else:
                login, token = credentials
                # Never alter global login or expose the PAT in argv. Credentials
                # remain repository-scoped in this account's private runtime tmpfs.
                with tempfile.TemporaryDirectory(prefix="homelab-image-auth-", dir=os.environ["XDG_RUNTIME_DIR"]) as temporary:
                    authfile = Path(temporary) / "auth.json"
                    encoded = base64.b64encode(f"{login}:{token}".encode("ascii")).decode("ascii")
                    authfile.touch(mode=0o600)
                    authfile.write_text(json.dumps({"auths": {repository: {"auth": encoded}}}))
                    try:
                        role_command("image-updates", tool("skopeo"), *arguments,
                                     "--authfile", str(authfile), *transports, timeout=1800)
                    except UpdateError as error:
                        raise UpdateError(str(error).replace(token, "[redacted]").replace(encoded, "[redacted]")) from None
                    except subprocess.TimeoutExpired:
                        raise UpdateError(f"registry image acquisition timed out for {image}") from None
            staging.chmod(0o600)
            validate_registry_archive(staging, image)
            with staging.open("rb") as completed:
                os.fsync(completed.fileno())
            os.replace(staging, archive)
            descriptor = os.open(cache, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        print(f"Acquired {image}")
    return 0


def role_command(role: str, *args: str, timeout: int = 600) -> str:
    result = run_as(role, list(args), text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise UpdateError(f"{role}: {Path(args[0]).name} failed: {result.stderr.strip()[-1200:]}")
    return result.stdout.strip()


def distribute_images(specification: Path, signature_policy: Path) -> int:
    require_admin()
    if not str(specification.resolve()).startswith("/nix/store/"):
        raise UpdateError("runtime image distribution requires an immutable packaged specification")
    services = json.loads(specification.read_text())
    needed = {}
    for service in services:
        role, image = service["principal"], service["image"]
        if principal_for_unit(service["unit"]) != role:
            raise UpdateError("image specification unit/principal mismatch")
        principal(role)
        if not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", image):
            raise UpdateError("runtime distribution requires immutable image references")
        if not image_present(role, image):
            needed.setdefault(image, set()).add(role)
    if needed:
        role_command("image-updates", tool("image_update"), "acquire",
                     "--signature-policy", str(signature_policy), *sorted(needed), timeout=3600)
    for image, targets in needed.items():
        for role in sorted(targets):
            stream_image("image-updates", role, image)
            if not image_present(role, image):
                raise UpdateError(f"{role}: OCI load did not retain the immutable registry reference")
            print(f"Distributed {image} to {role}")
    return 0


def activate_images(specification: Path, selected_principal: str | None = None) -> int:
    role = current_principal()
    if selected_principal is not None and selected_principal != role:
        raise UpdateError("image apply principal does not match the current account")
    active = []
    changed = []
    for service in json.loads(specification.read_text()):
        if selected_principal is not None and service["principal"] != role:
            continue
        unit = service["unit"]
        if service["principal"] != role or principal_for_unit(unit) != role:
            raise UpdateError("image apply may inspect only the current principal's declared units")
        state = role_command(role, tool("systemctl"), "--user", "show", "--property=ActiveState", "--value", unit)
        if state not in ("active", "reloading", "activating"):
            continue
        active.append(unit)
        if state == "activating":
            role_command(role, tool("systemctl"), "--user", "start", unit, timeout=1800)
        running = role_command(role, tool("podman"), "container", "inspect", "--format", "{{.Image}}", service["name"])
        expected = role_command(role, tool("podman"), "image", "inspect", "--format", "{{.Id}}", service["image"])
        # Index/platform pins or different compression can name identical
        # executable content. Compare config/rootfs identity, not aliases.
        if running.removeprefix("sha256:") != expected.removeprefix("sha256:"):
            changed.append(unit)
    if changed:
        print("Applying locked image changes: " + ", ".join(changed))
        role_command(role, tool("systemctl"), "--user", "restart", *changed, timeout=1800)
        # Restarting a dependency can stop its consumers. Restore only services
        # that were already running; never enroll manually stopped applications.
        role_command(role, tool("systemctl"), "--user", "start", *active, timeout=1800)
    else:
        print("Running containers already match the locked service images.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    home = Path.home()
    parser.add_argument("--feed", type=Path, default=os.environ.get("HOMELAB_IMAGE_FEED"),
                        help="Local image-feed checkout; required argument or HOMELAB_IMAGE_FEED for init/scan")
    parser.add_argument("--state-dir", type=Path, default=home / ".local/state/homelab-image-updates")
    parser.add_argument("--policy", type=Path, default=os.environ.get("HOMELAB_IMAGE_POLICY"))
    parser.add_argument("--seed", type=Path, default=os.environ.get("HOMELAB_IMAGE_SEED"))
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("init", help="Initialize the local Git input without upgrading existing pins")
    subparsers.add_parser("scan", help="Observe stable images and publish eligible immutable pins")
    subparsers.add_parser("status", help="Show observation deadlines without contacting registries")
    activation = subparsers.add_parser("activate", help="Apply locked image changes to already-running services")
    activation.add_argument("--spec", required=True, type=Path)
    activation.add_argument("--principal", help="Select one principal from an immutable whole-host specification")
    acquisition = subparsers.add_parser("acquire", help="Acquire locked images using private runtime GHCR credentials")
    acquisition.add_argument("--signature-policy", required=True, type=Path)
    acquisition.add_argument("images", nargs="*")
    distribution = subparsers.add_parser("distribute", help="Root/admin: stream missing locked images into service runtimes")
    distribution.add_argument("--spec", required=True, type=Path)
    distribution.add_argument("--signature-policy", required=True, type=Path)
    tree = subparsers.add_parser("tree-inputs", help="Observe or authorize upstream Tree build images, never local code")
    tree.add_argument("--dockerfile", type=Path)
    tree.add_argument("--dockerfile-stdin", action="store_true")
    tree.add_argument("--json", action="store_true", help="Return specification and rendered Dockerfile on stdout")
    tree.add_argument("--require-aged", action="store_true")
    tree.add_argument("--output", type=Path)
    tree.add_argument("--rendered-output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "distribute":
            return distribute_images(args.spec, args.signature_policy)
        if args.action == "activate":
            if not str(args.spec.resolve()).startswith("/nix/store/"):
                raise UpdateError("image apply requires an immutable packaged specification")
            roles = manifest()["principals"]
            if os.getuid() not in {identity["uid"] for identity in roles.values()}:
                require_admin()
                targets = sorted({service["principal"] for service in json.loads(args.spec.read_text())})
                if args.principal:
                    targets = [args.principal]
                for role in targets:
                    run_as(role, [tool("image_update"), "activate", "--spec", str(args.spec), "--principal", role],
                           check=True, timeout=3600)
                return 0
            return activate_images(args.spec, args.principal)
        if os.getuid() != principal("image-updates")["uid"]:
            require_admin()
            if args.action == "tree-inputs":
                if args.dockerfile_stdin:
                    dockerfile = sys.stdin.buffer.read()
                elif args.dockerfile:
                    dockerfile = args.dockerfile.read_bytes()
                else:
                    raise UpdateError("tree-inputs requires a Dockerfile")
                arguments = [tool("image_update"), "tree-inputs", "--dockerfile-stdin", "--json"]
                if args.require_aged:
                    arguments.append("--require-aged")
                result = run_as("image-updates", arguments, input=dockerfile, capture_output=True, timeout=600)
                if result.returncode:
                    raise UpdateError(result.stderr.decode("utf-8", "replace").strip())
                metadata = json.loads(result.stdout)
                if args.output:
                    atomic_json(args.output, metadata["specification"])
                if args.rendered_output:
                    args.rendered_output.write_text(metadata["rendered"])
                if args.json:
                    print(result.stdout.decode("utf-8").strip())
                return 0
            return run_as("image-updates", [tool("image_update"), *(sys.argv[1:] if argv is None else argv)]).returncode
        if args.action == "acquire":
            return acquire_images(args.images, args.signature_policy)
        # Activation must serialize with an existing observer, not fail because
        # that worker is still finishing the previous generation's scan.
        with locked(args.state_dir, wait=args.action == "init"):
            if args.action == "tree-inputs":
                with tempfile.TemporaryDirectory(prefix="tree-inputs-", dir=os.environ["XDG_RUNTIME_DIR"]) as temporary:
                    dockerfile = args.dockerfile
                    if args.dockerfile_stdin:
                        dockerfile = Path(temporary) / "Dockerfile"
                        dockerfile.write_bytes(sys.stdin.buffer.read())
                    if dockerfile is None:
                        raise UpdateError("tree-inputs requires a Dockerfile")
                    specification = collect_inputs(dockerfile, AgeStore(args.state_dir / "observations.json"),
                                                   require_aged=args.require_aged)
                    rendered = render_dockerfile(dockerfile, specification)
                if args.json:
                    print(json.dumps({"specification": specification, "rendered": rendered}))
                if args.rendered_output:
                    args.rendered_output.write_text(rendered)
                if args.output:
                    atomic_json(args.output, specification)
                return 0
            if args.action == "status":
                store = AgeStore(args.state_dir / "observations.json")
                for key, records in sorted(store.data["candidates"].items()):
                    for record in sorted(records.values(), key=lambda value: value["order"], reverse=True):
                        deadline = store.eligible_at(record["artifact"])
                        threshold = datetime.fromtimestamp(deadline, timezone.utc).isoformat() if deadline is not None else "unknown"
                        print(f"{key}: {record['version']} age threshold at {threshold} ({store.evidence_source(record['artifact'])})")
                for reference, artifacts in sorted(store.data.get("tree_inputs", {}).items()):
                    for artifact in artifacts.values():
                        deadline = store.eligible_at(artifact)
                        threshold = datetime.fromtimestamp(deadline, timezone.utc).isoformat() if deadline is not None else "unknown"
                        print(f"Tree base {reference}: {artifact['manifest']} age threshold at {threshold} ({store.evidence_source(artifact)})")
                return 0
            if args.policy is None or args.seed is None:
                raise UpdateError("--policy and --seed are required outside the packaged command")
            if args.feed is None:
                raise UpdateError("--feed or HOMELAB_IMAGE_FEED is required for init/scan")
            policy = read_policy(args.policy)
            if args.action == "init":
                initialize(args.feed, args.seed, policy)
                return 0
            return scan(args.feed, args.seed, policy, args.state_dir)
    except (PrincipalError, UpdateError, RegistryError, AgeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Image update blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
