"""Immutable service identity lookup and clean per-runtime management dispatch."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


class PrincipalError(RuntimeError):
    pass


def manifest() -> dict:
    path = Path(os.environ.get("HOMELAB_PRINCIPALS", "/etc/homelab/principals.json"))
    resolved = path.resolve(strict=True)
    if not (str(resolved).startswith("/nix/store/") or str(resolved).startswith("/etc/homelab/")):
        raise PrincipalError("principal manifest must be installed in /etc/homelab or the Nix store")
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise PrincipalError("principal manifest must be a root-owned, non-writable regular file")
    value = json.loads(resolved.read_text())
    if not isinstance(value.get("principals"), dict):
        raise PrincipalError("principal manifest has no enabled service identities")
    return value


def principal(name: str) -> dict:
    try:
        role = manifest()["principals"][name]
    except KeyError:
        raise PrincipalError(f"service principal is not enabled: {name}") from None
    if (
        role["name"] != name
        or role["account"] != f"homelab-{name}"
        or not isinstance(role["uid"], int)
        or not Path(role["home"]).is_absolute()
    ):
        raise PrincipalError(f"invalid service principal: {name}")
    return role


def principal_for_unit(unit: str) -> str:
    matches = [
        name
        for name, role in manifest()["principals"].items()
        if unit in role["units"] or unit in role.get("lifecycleUnits", [])
    ]
    if len(matches) != 1:
        raise PrincipalError(f"unit has no unique enabled service principal: {unit}")
    return matches[0]


def principals_for_component(component: str) -> list[str]:
    return sorted(name for name, role in manifest()["principals"].items() if component in role["components"])


def tool(name: str) -> str:
    defaults = {"sudo": "/run/wrappers/bin/sudo", "env": "/run/current-system/sw/bin/env"}
    path = os.environ.get(
        "HOMELAB_" + name.upper().replace("-", "_"), defaults.get(name, "/run/current-system/sw/bin/" + name)
    )
    if not Path(path).is_absolute():
        raise PrincipalError(f"{name} must be an absolute packaged executable")
    return path


def command_as(name: str, argv: list[str]) -> list[str]:
    role = principal(name)
    if not argv or not Path(argv[0]).is_absolute():
        raise PrincipalError("principal commands require an absolute executable")
    clean = [
        tool("env"),
        "--chdir=" + role["home"],
        "-i",
        f"HOME={role['home']}",
        f"USER={role['account']}",
        f"LOGNAME={role['account']}",
        f"XDG_RUNTIME_DIR=/run/user/{role['uid']}",
        "PATH=/run/wrappers/bin:/run/current-system/sw/bin",
        *map(str, argv),
    ]
    if os.getuid() == role["uid"]:
        return clean
    return [tool("sudo"), "-H", "-u", role["account"], "--", *clean]


def run_as(name: str, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    # env-i is also present for same-UID calls and streaming Popen consumers.
    # In particular no DBUS/SSH/keyring/authfile/token environment is forwarded.
    if "env" in kwargs:
        raise PrincipalError("principal dispatch does not accept an inherited environment")
    return subprocess.run(command_as(name, argv), **kwargs)


def current_principal() -> str:
    matches = [name for name, role in manifest()["principals"].items() if role["uid"] == os.getuid()]
    if len(matches) != 1:
        raise PrincipalError("this operation requires an enabled service account")
    return matches[0]


def require_admin() -> None:
    if os.getuid() not in (0, manifest()["operator"]["uid"]):
        raise PrincipalError("this operation requires root or the configured homelab operator")


def runtime_image_id(role: str, image: str) -> str:
    result = run_as(
        role,
        [tool("podman"), "image", "inspect", "--format", "{{.Id}}", image],
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise PrincipalError(f"{role}: could not inspect image identity")
    return result.stdout.strip()


def registry_archive_path(image: str) -> Path:
    """Locate the private, digest-preserving archive for an immutable registry image."""
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", image):
        raise PrincipalError("registry image archive requires an immutable reference")
    return (
        Path(principal("image-updates")["home"])
        / ".cache/homelab-registry-images"
        / (hashlib.sha256(image.encode()).hexdigest() + ".tar")
    )


def stream_image(source: str, target: str, image: str) -> str:
    """Transfer OCI content between clean UIDs without forwarding registry credentials."""
    registry = source == "image-updates" or "@" in image
    if registry:
        if source != "image-updates":
            raise PrincipalError("registry image transfer requires the image-updates source")
        producer_argv = [tool("cat"), str(registry_archive_path(image))]
        expected = None
    else:
        expected = runtime_image_id(source, image)
        producer_argv = [tool("podman"), "save", "--format=oci-archive", image]
    producer = consumer = None
    with tempfile.TemporaryFile() as save_error, tempfile.TemporaryFile() as load_error:
        try:
            producer = subprocess.Popen(command_as(source, producer_argv), stdout=subprocess.PIPE, stderr=save_error)
            consumer = subprocess.Popen(
                command_as(target, [tool("podman"), "load"]),
                stdin=producer.stdout,
                stdout=subprocess.DEVNULL,
                stderr=load_error,
            )
            producer.stdout.close()
            loaded = consumer.wait(timeout=1800)
            saved = producer.wait(timeout=1800)
            if saved or loaded:
                raise PrincipalError(
                    f"OCI transfer {source} -> {target} failed (producer={saved}, load={loaded}); source image retained"
                )
        finally:
            for process in (consumer, producer):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait()
            if producer is not None and producer.stdout is not None:
                producer.stdout.close()
    if registry:
        return runtime_image_id(target, image)
    if runtime_image_id(target, expected).removeprefix("sha256:") != expected.removeprefix("sha256:"):
        raise PrincipalError(f"{target}: OCI transfer changed image identity")
    return expected
