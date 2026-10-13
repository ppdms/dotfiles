#!/usr/bin/env python3
"""Edit homelab credential JSON in a locked, private tmpfs workspace."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field

import homelab_secrets as secrets
from principals import manifest, principal, principals_for_component, run_as


class CredsError(Exception):
    """A diagnostic constructed entirely from trusted constants."""


class Interrupted(CredsError):
    pass


def die(message: str) -> int:
    print("creds: " + message, file=sys.stderr)
    return 1


def runtime_root() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value or not Path(value).is_absolute():
        raise CredsError("XDG_RUNTIME_DIR must name your private tmpfs runtime directory")
    root = Path(value)
    info = root.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or root.resolve() != root
    ):
        raise CredsError("XDG_RUNTIME_DIR must be user-owned, mode 0700, and not a symlink")
    # Linux statfs starts with a native long filesystem type. Allocate more than
    # the complete structure, without depending on the remainder's ABI layout.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.statfs.argtypes = (ctypes.c_char_p, ctypes.c_void_p)
    libc.statfs.restype = ctypes.c_int
    filesystem = ctypes.create_string_buffer(256)
    if libc.statfs(os.fsencode(root), filesystem) != 0:
        raise CredsError("could not verify the runtime filesystem")
    if ctypes.c_long.from_buffer(filesystem).value != 0x01021994:
        raise CredsError("credential editing requires a tmpfs XDG_RUNTIME_DIR")
    return root


def editor_path(requested: str | None) -> tuple[str, str]:
    name = requested or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nvim"
    if name not in {"vi", "vim", "nvim", "nano"}:
        raise CredsError("editor must be a supported name: vi, vim, nvim, or nano (no command arguments)")
    try:
        configured = json.loads(Path(os.environ["HOMELAB_EDITORS"]).read_bytes())
        path = configured[name]
        if not isinstance(path, str) or not path.startswith("/nix/store/") or not os.access(path, os.X_OK):
            raise ValueError
    except (KeyError, OSError, ValueError, TypeError):
        raise CredsError("the packaged editor configuration is unavailable") from None
    return name, path


def run_editor(name: str, executable: str, plain: Path, workspace: Path) -> None:
    if name == "nano":
        # Ignoring configuration keeps backup, locking and search-history saving
        # disabled; restricted mode also forbids external commands and other files.
        command = [executable, "--ignorercfiles", "--restricted", str(plain)]
    else:
        command = [
            executable, "-u", "NONE", "--noplugin", "-n", "-i", "NONE",
            "--cmd", "set noswapfile nobackup nowritebackup noundofile nomodeline shadafile=NONE",
            str(plain),
        ]
    environment = os.environ.copy()
    for name in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        environment[name] = str(workspace)
    result = subprocess.run(command, env=environment, check=False)
    if result.returncode:
        raise CredsError("editor failed; encrypted credentials were not edited")


def read_edited(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise CredsError("edited credentials must remain a private mode-0600 regular file")
        return stream.read()


def systemctl(role: str, *arguments: str) -> subprocess.CompletedProcess:
    try:
        return run_as(
            role,
            [os.environ["HOMELAB_SYSTEMCTL"], "--user", *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, timeout=180,
        )
    except (KeyError, OSError, subprocess.TimeoutExpired):
        raise CredsError("the user service manager could not complete the requested operation") from None


def unit_state(role: str, unit: str) -> dict[str, str]:
    result = systemctl(role, "show", "--property=LoadState,UnitFileState,ActiveState", unit)
    if result.returncode:
        raise CredsError("could not inspect the required user units")
    try:
        properties = dict(line.split("=", 1) for line in result.stdout.decode("utf-8").splitlines())
    except (ValueError, UnicodeError):
        raise CredsError("the user service manager returned an invalid unit status") from None
    if not {"LoadState", "UnitFileState", "ActiveState"} <= properties.keys():
        raise CredsError("the user service manager returned an incomplete unit status")
    return properties


def available(unit: str, state: dict[str, str]) -> None:
    if state["LoadState"] != "loaded" or state["UnitFileState"] in {"masked", "masked-runtime"}:
        raise CredsError("refusing --restart: a required user unit is unavailable or masked")
    if state["ActiveState"] not in {"active", "reloading", "inactive", "failed"}:
        raise CredsError("refusing --restart while a required user unit is changing state")


def homelab_active(role: str) -> None:
    state = unit_state(role, "homelab.target")
    available("homelab.target", state)
    if state["ActiveState"] != "active":
        raise CredsError("refusing --restart: homelab.target is not active")


def running(state: dict[str, str]) -> bool:
    return state["ActiveState"] in {"active", "reloading", "activating", "deactivating"}


@dataclass
class RestartPlan:
    component: str
    active: list[tuple[str, str]] = field(default_factory=list)
    attempted: list[tuple[str, str]] = field(default_factory=list)

    @property
    def selected(self) -> list[tuple[str, str]]:
        return [
            (name, unit)
            for name in principals_for_component(self.component)
            for unit in principal(name)["units"]
            if not unit.endswith("-migrate.service")
        ]

    def stop(self) -> None:
        selected = self.selected
        if not selected:
            raise CredsError("no enabled service runtime consumes this credential")
        candidates = selected[:]
        if self.component != "nginx" and "nginx" in manifest()["principals"]:
            candidates.append(("nginx", "nginx.service"))
        # Inspect the complete family before changing any runtime. Manual Tree
        # dev and inactive sidecars remain inactive, including after a successful edit.
        for role, unit in candidates:
            state = unit_state(role, unit)
            available(unit, state)
            if running(state):
                homelab_active(role)
                self.active.append((role, unit))
        # Stop before acquiring the credential lock: ExecStopPost persists runtime
        # state under the same lock. Stop the proxy first, so consumers cannot race.
        for role, unit in reversed(self.active):
            self.attempted.append((role, unit))
            if systemctl(role, "stop", unit).returncode:
                raise CredsError("could not stop the selected credential consumer family")

    def resume(self) -> bool:
        restored = True
        for role, unit in reversed(self.attempted):
            try:
                homelab_active(role)
                available(unit, unit_state(role, unit))
                if systemctl(role, "start", unit).returncode:
                    restored = False
            except Exception:
                restored = False
        return restored


def missing_fields(component: str, fields: dict[str, str]) -> list[str]:
    return sorted(name for name in secrets.required_fields(component) if not fields.get(name))


def without_unset_optional(component: str, fields: dict[str, str]) -> dict[str, str]:
    optional = secrets.OPTIONAL.get(component, ())
    return {name: value for name, value in fields.items() if value or name not in optional}


def edit(component: str, check: bool, editor: tuple[str, str] | None, root: Path | None) -> bool:
    if check:
        # A check has no delivery/persistence/lock-file or service lifecycle effects.
        fields = secrets.load_fields(component)
        try:
            secrets.parse_payload(component, secrets.payload_bytes(component, fields), complete=True)
        except ValueError:
            missing = missing_fields(component, fields)
            if missing:
                raise CredsError("missing required fields for " + component + ": " + ", ".join(missing)) from None
            raise CredsError("credential JSON or field contents are invalid") from None
        print("Credential for " + component + " is decryptable and valid.")
        return False
    try:
        lock = secrets.component_lock(component, nonblocking=True)
        with lock as directory:
            # Mutable runtime state belongs to the application's private runtime,
            # never the operator's editor workspace. --restart stop hooks flush it.
            fields, expected_digest = secrets.load_record(component)
            digest = secrets.runtime_digest(component, directory)
            template = {name: fields.get(name, "") for name in secrets.FIELDS[component]}
            payload = {"version": 1, "services": {component: {"env": {}, "files": template}}}
            with tempfile.TemporaryDirectory(prefix="creds-" + component + "-", dir=root) as temporary:
                workspace = Path(temporary)
                workspace.chmod(0o700)
                plain = workspace / "credentials.json"
                descriptor = os.open(plain, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=True)
                    stream.write("\n")
                assert editor is not None
                run_editor(*editor, plain, workspace)
                try:
                    updated = without_unset_optional(component, secrets.parse_payload(component, read_edited(plain), complete=True))
                except ValueError:
                    raise CredsError("credential JSON or field contents are invalid; encrypted credentials were not edited") from None
                if updated == fields:
                    print("Credential for " + component + " was not changed.")
                    return False
                if secrets.runtime_digest(component, directory) != digest:
                    raise CredsError("runtime credentials changed during editing; no edit saved; retry after stopping the component")
                mutable = secrets.MUTABLE.get(component)
                if mutable and updated.get(mutable) != fields.get(mutable):
                    if any(running(unit_state(role, unit)) for role, unit in RestartPlan(component).selected):
                        raise CredsError("cannot change the mutable runtime field while the component is running; stop it or use --restart")
                secrets.store_fields(component, updated, expected_digest=expected_digest)
                print("Updated credential for " + component + "; previous encrypted version retained when present.")
                return True
    except BlockingIOError:
        raise CredsError("another credential operation is active for " + component) from None


def interrupt(signum: int, frame: object) -> None:
    raise Interrupted("interrupted; the credential edit was not completed")


@contextmanager
def signal_handlers():
    previous = {}
    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, interrupt)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="creds", description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate all required encrypted fields without opening an editor")
    parser.add_argument("--restart", action="store_true", help="stop the running consumer family and proxy, edit, then restore their prior running state")
    parser.add_argument("--editor", metavar="NAME", help="vi, vim, nvim (packaged Neovim), or nano; default VISUAL/EDITOR or nvim")
    parser.add_argument("component", choices=tuple(secrets.FIELDS))
    args = parser.parse_args(argv)
    if args.check and args.restart:
        return die("--check and --restart cannot be combined")
    if args.restart and args.component in {"image-updates", "vpn-ddns"}:
        return die("this native credential is read fresh on the next scheduled invocation and needs no restart")
    if os.geteuid() == 0:
        return die("creds requires the configured homelab operator account, not root")
    os.umask(0o077)
    plan = RestartPlan(args.component) if args.restart else None
    success = False
    saved = False
    message = None
    resumed = True
    with signal_handlers():
        try:
            if os.getuid() != manifest()["operator"]["uid"]:
                raise CredsError("credential administration requires the configured homelab operator")
            root = None if args.check else runtime_root()
            editor = None if args.check else editor_path(args.editor)
            if plan:
                plan.stop()
            saved = edit(args.component, args.check, editor, root)
            success = True
        except CredsError as exc:
            message = str(exc)
        except Exception:
            # Neither parser failures, broker errors nor raw command output are
            # allowed to interpolate credential contents into terminal diagnostics.
            message = "credential operation failed; check input format, private runtime files, broker authorization and concurrent edits"
        finally:
            if plan:
                # A second termination request must not interrupt state restoration.
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    signal.signal(number, signal.SIG_IGN)
                resumed = plan.resume()
    if message:
        die(message)
    if not resumed:
        if success and saved:
            die("credentials were saved, but service startup failed or a homelab target was inactive; encrypted broker credentials are retained")
        elif success:
            die("credentials were unchanged, but service startup failed or the homelab target was inactive")
        else:
            die("the previous running service state could not be restored; check the homelab target and user unit status")
        return 1
    if message:
        return 1
    if plan:
        print("Restored the credential consumer family and proxy only where they were previously running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
