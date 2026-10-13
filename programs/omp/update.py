import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request


LATEST_RELEASE = "https://api.github.com/repos/can1357/oh-my-pi/releases/latest"
ASSET_NAME = "omp-linux-x64"
VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class UpdateError(Exception):
    pass


def version_parts(version):
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise UpdateError("Release version must be a stable numeric MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in version.split("."))


def sha256_bytes(hash_value):
    if not isinstance(hash_value, str) or not hash_value.startswith("sha256-"):
        raise UpdateError("Expected a SHA256 hash in Nix SRI format")
    try:
        digest = base64.b64decode(hash_value[7:], validate=True)
    except ValueError as error:
        raise UpdateError("Invalid SHA256 hash in Nix SRI format") from error
    if len(digest) != 32:
        raise UpdateError("Invalid SHA256 hash length")
    return digest


def latest_release():
    request = urllib.request.Request(
        LATEST_RELEASE,
        headers={
            "User-Agent": "homelab-omp-update",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        release = json.load(response)
    if not isinstance(release, dict):
        raise UpdateError("GitHub returned invalid release metadata")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("GitHub latest release is not a stable vMAJOR.MINOR.PATCH tag")
    version = tag[1:]
    version_parts(version)
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise UpdateError("GitHub latest release is a draft or prerelease")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("GitHub release has no asset list")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == ASSET_NAME]
    if len(matches) != 1:
        raise UpdateError(f"GitHub release must contain exactly one {ASSET_NAME} asset")
    digest = matches[0].get("digest")
    if digest is not None and not isinstance(digest, str):
        raise UpdateError("GitHub asset has an invalid digest")
    if digest is not None and digest.startswith("sha256:"):
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise UpdateError("GitHub asset has an invalid SHA256 digest")
        digest = bytes.fromhex(digest[7:])
    else:
        digest = None
    # Never use a download URL supplied by the API to select a different asset.
    url = f"https://github.com/can1357/oh-my-pi/releases/download/{tag}/{ASSET_NAME}"
    return version, url, digest


def command_output(command):
    return subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE).stdout


def prefetch_release(nix, url, expected_digest):
    result = json.loads(command_output([
        nix, "store", "prefetch-file", "--json", "--hash-type", "sha256",
        "--name", ASSET_NAME, url,
    ]))
    if not isinstance(result, dict):
        raise UpdateError("Nix prefetch returned invalid metadata")
    hash_value = result.get("hash")
    digest = sha256_bytes(hash_value)
    store_path = result.get("storePath")
    if not isinstance(store_path, str) or not Path(store_path).is_absolute():
        raise UpdateError("Nix prefetch did not return an absolute store path")
    if expected_digest is not None and digest != expected_digest:
        raise UpdateError("Prefetched asset SHA256 does not match GitHub's release digest")
    return hash_value


def atomic_write(path, content, mode):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore_pin(path, original, replacement, mode):
    # Editors need not take the updater lock. Do not undo their independent edit.
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        print("OMP pin was removed independently; it was not restored.", file=sys.stderr)
        return
    if current != replacement:
        print("OMP pin was edited independently; that edit was left untouched.", file=sys.stderr)
        return
    atomic_write(path, original, mode)
    print("Restored the original OMP release pin.", file=sys.stderr)


def build_package(nix, checkout):
    output = command_output([
        nix, "build", "--no-update-lock-file", "--no-link", "--print-out-paths",
        f"{checkout}#omp",
    ])
    outputs = output.splitlines()
    if len(outputs) != 1 or not outputs[0] or not Path(outputs[0]).is_absolute():
        raise UpdateError("OMP build must return exactly one absolute output path")
    package = Path(outputs[0])
    if not (package / "bin" / "omp").is_file() or not os.access(package / "bin" / "omp", os.X_OK):
        raise UpdateError("OMP build output does not contain an executable bin/omp")
    return outputs[0]


def update(checkout, profile, nix, nix_env):
    checkout = Path(checkout).expanduser().resolve()
    # Do not resolve the profile symlink: nix-env must replace that symlink itself.
    profile = Path(profile).expanduser().absolute()
    if not (checkout / ".git").exists() or not (checkout / "flake.nix").is_file():
        raise UpdateError("--checkout must name the root of a local Git flake checkout")
    pin_path = checkout / "tools" / "omp-source.json"
    profile.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{profile}.update.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = pin_path.read_bytes()
        mode = stat.S_IMODE(pin_path.stat().st_mode)
        source = json.loads(original)
        if not isinstance(source, dict):
            raise UpdateError("OMP release pin must be a JSON object")
        current_version = source.get("version")
        current_parts = version_parts(current_version)
        sha256_bytes(source.get("hash"))
        version, url, expected_digest = latest_release()
        if version_parts(version) < current_parts:
            print(f"OMP pin {current_version} is newer than GitHub latest {version}; no downgrade or profile change attempted.")
            return
        hash_value = prefetch_release(nix, url, expected_digest)
        replacement_source = dict(source)
        replacement_source.update(version=version, hash=hash_value)
        replacement = original if replacement_source == source else (json.dumps(replacement_source, indent=2) + "\n").encode()
        if pin_path.read_bytes() != original:
            raise UpdateError("OMP pin changed independently while fetching the release; no pin or profile change attempted")
        changed = replacement != original
        try:
            if changed:
                atomic_write(pin_path, replacement, mode)
            output = build_package(nix, checkout)
            if pin_path.read_bytes() != replacement:
                raise UpdateError("OMP pin changed independently during the build; profile installation was not attempted")
            # Activate exactly the checked build output, with no second evaluation.
            subprocess.run([nix_env, "--profile", str(profile), "--set", output], check=True)
        except BaseException:
            if changed:
                try:
                    restore_pin(pin_path, original, replacement, mode)
                except OSError as error:
                    print(f"Could not restore the original OMP pin: {error}", file=sys.stderr)
            raise
        if changed:
            print(f"OMP release pin: {current_version} -> {version} ({hash_value}).")
        else:
            print(f"OMP release pin remains {version}; rebuilt using the checkout's pinned dependencies.")
        print(f"OMP profile: {profile} -> {output}")
        print("New OMP invocations use the new version; the running OMP process was not restarted.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Update only the OMP release pin and its dedicated Nix profile.")
    parser.add_argument("--checkout", required=True, help="Root of the local Git flake checkout")
    parser.add_argument("--profile", required=True, help="Dedicated single-package OMP Nix profile")
    args = parser.parse_args(argv)
    try:
        nix = os.environ.get("HOMELAB_NIX")
        nix_env = os.environ.get("HOMELAB_NIX_ENV")
        if not nix or not nix_env:
            raise UpdateError("Run the packaged homelab-omp-update command with its configured Nix binaries")
        update(args.checkout, args.profile, nix, nix_env)
        return 0
    except KeyboardInterrupt:
        print("OMP update interrupted.", file=sys.stderr)
        return 130
    except subprocess.CalledProcessError as error:
        print(f"OMP update failed: {Path(error.cmd[0]).name} exited with status {error.returncode}.", file=sys.stderr)
        return 1
    except (UpdateError, OSError, ValueError) as error:
        print(f"OMP update failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
