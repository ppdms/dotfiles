#!/usr/bin/env python3
"""Tern for the homelab: Stencil sign-in, release updates and the web token.

Every secret lives in the encrypted credential broker: Stencil's refresh token in
the operator-only `tern` component, and the /wire token in the optional
omp-web-token.conf field of the `nginx` component (nginx includes it, tern-web
reads the same value, so there is one copy).

Stencil refresh tokens are single-use and reuse revokes the whole session, so a
token is saved back before anything else uses what it returned.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets as token_source
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

import homelab_secrets as store
from credential_client import CredentialError
from credential_validation import web_token_from_snippet, web_token_snippet

AUTH = "https://auth.stencil.so"
BUILD = "https://build.stencil.so"
CLIENT_ID = "tern"
SCOPE = "openid profile email offline_access"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
PLATFORM = "linux-x86_64"
SESSION = ("tern", "stencil-refresh-token")
WEB_TOKEN = ("nginx", "omp-web-token.conf")
BUILD_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{7,40}")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SAVE_ATTEMPTS = 5

sleep = time.sleep
monotonic = time.monotonic


class ToolError(Exception):
    pass


def version_parts(version):
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ToolError("Stencil reported a version that is not a stable MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in version.split("."))


def sri(hex_digest):
    return "sha256-" + base64.b64encode(bytes.fromhex(hex_digest)).decode()


# --- HTTP ---------------------------------------------------------------------
# curl, not urllib: Stencil's Cloudflare refuses Python's default client
# signature (error 1010). Every secret travels on curl's stdin as a config file,
# never in argv, where other local accounts could read it.

def quote(value):
    if not isinstance(value, str) or any(character in value for character in "\0\r\n"):
        raise ToolError("refusing to send a value with control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def http(url, *, form=None, bearer=None, output=None, follow=False, timeout=60):
    """Return (status, body); with output=, the body goes to that file instead."""
    lines = [
        "silent", "show-error", 'proto = "=https"', 'proto-redir = "=https"',
        f"max-time = {int(timeout)}", f"url = {quote(url)}", 'write-out = "%{http_code}"',
    ]
    if bearer is not None:
        lines.append("header = " + quote("Authorization: Bearer " + bearer))
    for name, value in (form or {}).items():
        lines.append("data-urlencode = " + quote(f"{name}={value}"))
    if follow:
        lines.append("location")
    if output is not None:
        lines.append("output = " + quote(str(output)))
    result = subprocess.run(
        [os.environ.get("HOMELAB_CURL", "curl"), "--config", "-"],
        input=("\n".join(lines) + "\n").encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    host = url.split("/")[2]
    if result.returncode:
        raise ToolError(f"could not reach {host} (curl exit {result.returncode})")
    try:
        status = int(result.stdout[-3:])
    except ValueError:
        raise ToolError(f"{host} returned no HTTP status") from None
    return status, result.stdout[:-3]


def json_object(body, what):
    try:
        value = json.loads(body)
    except ValueError:
        raise ToolError(f"{what} did not return JSON (an HTML page usually means a blocked or signed-out client)") from None
    if not isinstance(value, dict):
        raise ToolError(f"{what} returned an unexpected JSON shape")
    return value


def token_request(form):
    status, body = http(AUTH + "/oauth/token", form={**form, "client_id": CLIENT_ID})
    return status, json_object(body, "the Stencil sign-in server")


# --- the saved session --------------------------------------------------------

def save_token(token):
    """Persist a (rotated) refresh token; compare-and-swap, retried, then verified.

    "Saved or not" can be ambiguous after a broker error, so every attempt first
    re-reads the record and treats an already-present token as success.
    """
    component, field = SESSION
    failure = None
    for attempt in range(SAVE_ATTEMPTS):
        try:
            fields, digest = store.load_record(component)
            if fields.get(field) == token:
                return
            store.store_fields(component, {**fields, field: token}, expected_digest=digest)
            return
        except ValueError as error:
            raise ToolError(f"Stencil's token was not saved: {error}") from None
        except CredentialError as error:
            failure = error
            sleep(min(2 ** attempt, 10))
    raise ToolError(
        f"Stencil issued a new session token that could not be saved ({failure}); "
        "the previous one is already revoked. Run: homelab-tern login"
    )


def refresh():
    """Trade the saved refresh token for an access token, saving its successor first."""
    component, field = SESSION
    token = store.load_fields(component).get(field)
    if not token:
        raise ToolError("not signed in to Stencil. Run: homelab-tern login")
    status, body = token_request({"grant_type": "refresh_token", "refresh_token": token})
    if status != 200:
        if body.get("error") == "invalid_grant":
            raise ToolError(
                f"Stencil rejected the saved session ({body.get('error_description', 'invalid_grant')}). "
                "Run: homelab-tern login"
            )
        raise ToolError(f"Stencil refused the session refresh (HTTP {status})")
    successor, access = body.get("refresh_token"), body.get("access_token")
    if not isinstance(successor, str) or not isinstance(access, str) or not access:
        raise ToolError("Stencil's refresh answer lacks tokens")
    save_token(successor)
    return access


def login():
    with store.component_lock(SESSION[0], nonblocking=True):
        status, grant = http(AUTH + "/oauth/device_authorization", form={"client_id": CLIENT_ID, "scope": SCOPE})
        grant = json_object(grant, "the Stencil sign-in server")
        needed = ("device_code", "user_code", "verification_uri", "expires_in")
        if status != 200 or any(key not in grant for key in needed):
            raise ToolError(f"Stencil refused to start a sign-in (HTTP {status})")
        link = grant.get("verification_uri_complete") or grant["verification_uri"]
        print(f"Open {link}\nconfirm the code {grant['user_code']} with your Stencil account.", file=sys.stderr)
        deadline = monotonic() + min(int(grant["expires_in"]), 1800)
        interval = max(int(grant.get("interval", 5)), 1)
        while monotonic() < deadline:
            sleep(interval)
            status, body = token_request({"grant_type": DEVICE_GRANT, "device_code": grant["device_code"]})
            error = body.get("error")
            if status == 200:
                refresh_token = body.get("refresh_token")
                if not isinstance(refresh_token, str):
                    raise ToolError("Stencil's sign-in returned no refresh token; the offline_access scope was not granted")
                save_token(refresh_token)
                print("Signed in; the Stencil session is saved in the encrypted credential store.")
                return
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "access_denied":
                raise ToolError("the sign-in was declined")
            if error == "expired_token":
                break
            raise ToolError(f"Stencil refused the sign-in ({error or 'HTTP ' + str(status)})")
        raise ToolError("the code expired before it was confirmed; run login again")


# --- release updates ----------------------------------------------------------

def latest_build(access):
    status, body = http(f"{BUILD}/api/products/tern/latest/{PLATFORM}", bearer=access)
    answer = json_object(body, "the Stencil build service")
    if status in (401, 403):
        raise ToolError(f"the build service refused the session ({answer.get('error', status)}); this account may lack Tern access")
    if status != 200 or not isinstance(answer.get("build"), dict):
        raise ToolError(f"the build service answered HTTP {status}")
    build = answer["build"]
    version = build.get("version")
    version_parts(version)
    if build.get("channel") != "stable":
        raise ToolError("the latest build is not on the stable channel")
    identifier = build.get("id")
    if not isinstance(identifier, str) or not BUILD_ID.fullmatch(identifier):
        raise ToolError("the build has an unexpected id")
    name = f"Tern-{version}-{PLATFORM}.tar.gz"
    files = [item for item in build.get("files", []) if isinstance(item, dict) and item.get("name") == name]
    if len(files) != 1:
        raise ToolError(f"the build must list exactly one {name}")
    item = files[0]
    digest, size = item.get("sha256"), item.get("size")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or type(size) is not int or size <= 0:
        raise ToolError("the build lists an invalid size or SHA-256")
    # Never trust a URL from the API to point anywhere but this build's file.
    return {"version": version, "name": name, "sha256": digest, "size": size,
            "url": f"{BUILD}/d/tern/{identifier}/{name}"}


def command_output(argv, check=True):
    result = subprocess.run(argv, stdout=subprocess.PIPE, text=True, check=False)
    if check and result.returncode:
        raise ToolError(f"{Path(argv[0]).name} exited with status {result.returncode}")
    return result


def in_store(name, digest):
    store_tool = os.environ.get("HOMELAB_NIX_STORE", "nix-store")
    path = command_output([store_tool, "--print-fixed-path", "sha256", digest, name]).stdout.strip()
    return command_output([store_tool, "--check-validity", path], check=False).returncode == 0


def pin_state(path):
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                raise ToolError("the Tern pin must be a regular file")
            content = stream.read()
            after = os.fstat(stream.fileno())
            if ((after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_mode) !=
                    (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode) or
                    path.stat().st_ino != info.st_ino):
                raise ToolError("the Tern pin changed independently while it was being read")
    except FileNotFoundError:
        return None
    return (content, stat.S_IMODE(info.st_mode), info.st_dev, info.st_ino,
            info.st_mtime_ns)


def read_pin(path):
    original = pin_state(path)
    if original is None:
        return None, original
    try:
        pin = json.loads(original[0])
    except ValueError:
        raise ToolError(f"{path.name} is not valid JSON") from None
    if not isinstance(pin, dict) or not isinstance(pin.get("version"), str) or not isinstance(pin.get("hash"), str):
        raise ToolError(f"{path.name} must hold a version and a hash")
    return pin, original


def download(build, access, directory):
    target = Path(directory) / build["name"]
    status, _ = http(build["url"], bearer=access, output=target, follow=True, timeout=1800)
    if status != 200:
        raise ToolError(f"the download answered HTTP {status}")
    hasher = hashlib.sha256()
    with open(target, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(block)
    if target.stat().st_size != build["size"] or hasher.hexdigest() != build["sha256"]:
        target.unlink(missing_ok=True)
        raise ToolError("the download does not match Stencil's published size and SHA-256; it was discarded")
    return target


def verify_archive(path):
    seen = set()
    try:
        with tarfile.open(path, mode="r|gz") as archive:
            for member in archive:
                name = member.name.removeprefix("./")
                if name not in ("tern", "tern/tern") or name in seen:
                    raise ToolError("the Tern archive contains unexpected or duplicate members")
                seen.add(name)
                if name == "tern":
                    if not member.isdir():
                        raise ToolError("the Tern archive root must be a directory")
                elif member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.size <= 0 or not member.mode & 0o111:
                    raise ToolError("the Tern archive must contain a nonempty regular executable tern/tern")
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ToolError(f"the Tern download is not a valid gzip tarball: {error}") from None
    if "tern/tern" not in seen:
        raise ToolError("the Tern archive must contain tern/tern")


def require_pin_state(path, expected):
    if pin_state(path) != expected:
        raise ToolError("the Tern pin changed independently; the independent edit was left untouched")


def atomic_pin(path, content, mode, expected):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
            replacement = (content, mode, info.st_dev, info.st_ino, info.st_mtime_ns)
        require_pin_state(path, expected)
        os.replace(temporary, path)
        return replacement
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore_pin(path, original, replacement):
    # Editors do not take our lock: never undo somebody else's edit.
    if pin_state(path) != replacement:
        print("The Tern pin was edited independently; left untouched.", file=sys.stderr)
        return
    if original is None:
        require_pin_state(path, replacement)
        path.unlink()
    else:
        atomic_pin(path, original[0], original[1], replacement)
    print("Restored the previous Tern pin.", file=sys.stderr)


def update(checkout, check_only):
    checkout = Path(checkout).expanduser().resolve()
    pin_path = checkout / "programs" / "tern" / "source.json"
    if not (checkout / ".git").exists() or not (checkout / "flake.nix").is_file():
        raise ToolError("--checkout must be the root of the dotfiles Git checkout")
    with store.component_lock(SESSION[0], nonblocking=True):
        pin, original = read_pin(pin_path)
        access = refresh()
        build = latest_build(access)
        published = sri(build["sha256"])
        if pin is not None:
            if version_parts(build["version"]) < version_parts(pin["version"]):
                require_pin_state(pin_path, original)
                print(f"Pinned Tern {pin['version']} is newer than Stencil's latest {build['version']}; nothing to do.")
                return
            if build["version"] == pin["version"] and pin["hash"] != published:
                raise ToolError(f"Stencil now publishes a different file for Tern {build['version']} than the pin; refusing to replace it")
        current = pin is not None and pin["version"] == build["version"]
        print(f"Tern pinned: {pin['version'] if pin else 'none'}; Stencil latest: {build['version']}.")
        if check_only:
            require_pin_state(pin_path, original)
            return
        if current and in_store(build["name"], build["sha256"]):
            require_pin_state(pin_path, original)
            print("The pinned release is already in the Nix store; nothing to do.")
            return
        with tempfile.TemporaryDirectory(prefix="homelab-tern-") as scratch:
            tarball = download(build, access, scratch)
            verify_archive(tarball)
            require_pin_state(pin_path, original)
            store_tool = os.environ.get("HOMELAB_NIX_STORE", "nix-store")
            command_output([store_tool, "--add-fixed", "sha256", str(tarball)])
        require_pin_state(pin_path, original)
        content = original[0] if current else (json.dumps({
            "version": build["version"], "hash": published,
        }, indent=2) + "\n").encode()
        replacement = original
        try:
            if not current:
                replacement = atomic_pin(pin_path, content, original[1] if original else 0o644, original)
            nix = os.environ.get("HOMELAB_NIX", "nix")
            output = command_output([nix, "build", "--no-update-lock-file", "--no-link",
                                     "--print-out-paths", f"{checkout}#tern"]).stdout.split()
            if len(output) != 1 or not Path(output[0]).is_absolute():
                raise ToolError("the Tern build must produce exactly one output")
            require_pin_state(pin_path, replacement)
        except BaseException:
            if replacement != original:
                try:
                    restore_pin(pin_path, original, replacement)
                except (OSError, ToolError) as error:
                    print(f"Could not restore the previous Tern pin: {error}", file=sys.stderr)
            raise
        print(f"Tern {build['version']} is pinned and verified. Commit programs/tern/source.json and run rebuild;")
        print("the running tern-web keeps its current version until the switch restarts it.")


# --- the web token ------------------------------------------------------------

def web_token_generate(rotate):
    component, field = WEB_TOKEN
    with store.component_lock(component, nonblocking=True):
        fields, digest = store.load_record(component)
        if fields.get(field) and not rotate:
            raise ToolError("a web token already exists; --rotate replaces it")
        store.store_fields(component, {**fields, field: web_token_snippet(token_source.token_hex(32))}, expected_digest=digest)
    print("Stored a new web token. Restart both ends so they agree:")
    print("  systemctl --user restart tern-web")
    print("  sudo systemctl --user -M homelab-nginx@ restart nginx.service")


def web_token_write(output, wait):
    component, field = WEB_TOKEN
    deadline = monotonic() + wait
    while True:
        try:
            snippet = store.load_fields(component).get(field)
            break
        except CredentialError:
            # At boot the user manager can beat the credential broker.
            if monotonic() >= deadline:
                raise
            sleep(2)
    if not snippet:
        raise ToolError("no web token is stored. Run: homelab-tern web-token generate")
    target = Path(output)
    store.private_directory(target.parent)
    # Tern writes its own token file with a trailing newline; match it.
    store.atomic_write(target, (web_token_from_snippet(snippet) + "\n").encode())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="homelab-tern", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="sign in to Stencil with a device code and save the session")
    updating = commands.add_parser("update", help="fetch the latest Tern, verify it, pin it and test-build it")
    updating.add_argument("--checkout", default=os.environ.get("HOMELAB_CHECKOUT"), help="dotfiles checkout (default: this host's)")
    updating.add_argument("--check", action="store_true", help="only compare the pin with Stencil's latest")
    token = commands.add_parser("web-token", help="manage the token nginx adds to Tern's /wire")
    actions = token.add_subparsers(dest="action", required=True)
    generating = actions.add_parser("generate", help="store a fresh random token in the nginx credential")
    generating.add_argument("--rotate", action="store_true", help="replace an existing token")
    writing = actions.add_parser("write", help="write the stored token to a private file for tern web serve")
    writing.add_argument("path")
    writing.add_argument("--wait", type=int, default=120, help="seconds to wait for the credential broker")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "login":
            login()
        elif args.command == "update":
            if not args.checkout:
                raise ToolError("--checkout is required")
            update(args.checkout, args.check)
        elif args.action == "generate":
            web_token_generate(args.rotate)
        else:
            web_token_write(args.path, args.wait)
        return 0
    except KeyboardInterrupt:
        print("homelab-tern: interrupted", file=sys.stderr)
        return 130
    except KeyError as error:
        print(f"homelab-tern: missing environment variable {error}", file=sys.stderr)
        return 1
    except (ToolError, CredentialError, ValueError, TypeError, OSError) as error:
        print(f"homelab-tern: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"homelab-tern: {Path(error.cmd[0]).name} failed (status {error.returncode})", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(f"homelab-tern: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
