#!/usr/bin/env python3
"""Convert private Darwin metadata into validated machine settings."""
import argparse
import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import uuid


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Private source JSON")
    parser.add_argument("--checkout", type=Path, required=True, help="Explicit public writable checkout on the Darwin host")
    parser.add_argument("--darwin-state-version", type=int, required=True,
                        help="nix-darwin compatibility integer (1..6), not a release string")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("NIX_CONFIG_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "nix-config")))
    parser.add_argument("--backrest-config", type=Path, help="Private runtime Backrest JSON; same environment/default path as profile")
    parser.add_argument("--patch", type=Path, help="Private JSON Merge Patch of the output document (RFC 7396)")
    parser.add_argument("--identity", type=Path, help="Existing SOPS identity for --seal")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path, help="Write validated private JSON for review, without sealing or rendering")
    action.add_argument("--seal", action="store_true", help="Back up existing SOPS ciphertext, then seal and render; never activate")
    args = parser.parse_args()
    if not 1 <= args.darwin_state_version <= 6:
        parser.error("--darwin-state-version must be the actual nix-darwin integer in 1..6, not a release string")
    if not args.checkout.is_absolute():
        parser.error("--checkout must be an explicit absolute path on the Darwin host")
    return args


def merge_patch(target, patch):
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def object_at(value, key, helper):
    item = value.get(key)
    if not isinstance(item, dict):
        helper.fail(key, "missing existing object; supply actual metadata, not an invented identity")
    return item


def convert(source, args, helper):
    profiles = object_at(source, "profiles", helper)
    darwin = object_at(profiles, "darwin", helper)
    system = object_at(darwin, "system", helper)
    user = object_at(darwin, "user", helper)
    applications = object_at(darwin, "applications", helper)
    contract = helper.Contract()
    properties = contract.schema["$defs"]["darwin"]["properties"]
    selected = {}
    for field in ("system", "user", "applications", "network"):
        existing = darwin.get(field, {})
        if not isinstance(existing, dict):
            helper.fail(field, "expected an existing object")
        allowed = properties[field]["properties"]
        selected[field] = {key: copy.deepcopy(value) for key, value in existing.items() if key in allowed}
    # Platform and development defaults; compatibility requires an explicit integer.
    selected["system"].update({
        "platform": "aarch64-darwin", "role": "laptop",
        "homeStateVersion": system.get("homeStateVersion", "25.05"),
        "systemStateVersion": args.darwin_state_version, "checkout": str(args.checkout),
    })
    selected["user"].setdefault("signCommits", True)
    if "email" in user:
        selected["user"].setdefault("templateEmail", user["email"])
    selected["development"] = {"nodeMajor": 22, "pythonMinor": 13}
    selected["ssh"] = copy.deepcopy(object_at(darwin, "ssh", helper))
    # An existing private OMP block is preserved exactly, not replaced by
    # public account/model defaults. Validation rejects unsupported settings.
    if "omp" in applications:
        selected["applications"]["omp"] = copy.deepcopy(applications["omp"])
    result = copy.deepcopy(source)
    result["profiles"]["darwin"] = selected
    archive_key = "sourceDarwinProfile"
    if archive_key not in result:
        result[archive_key] = copy.deepcopy(darwin)
    # Capture editable runtime files from private state, as seal does.
    files = result.get("files", {})
    if not isinstance(files, dict):
        helper.fail("files", "expected a local file map")
    for key, relative in [("vscodeSshConfig", "vscode_ssh_config"), ("syncthingConfig", "syncthing/config.xml")]:
        path = args.state_dir / relative
        if path.exists():
            files[key] = helper.read_file(path, "local file").decode("utf-8")
    if files:
        result["files"] = files
    helper.capture_backrest(result, args)
    if args.patch:
        patch = helper.parse_json(helper.read_file(args.patch, "private metadata patch"), "private metadata patch")
        if not isinstance(patch, dict):
            helper.fail("patch", "expected an object JSON Merge Patch")
        result = merge_patch(result, patch)
    # A private patch may change real identity/application metadata but must
    # not silently override the explicit compatibility/checkout arguments.
    current = object_at(object_at(object_at(result, "profiles", helper), "darwin", helper), "system", helper)
    if current.get("systemStateVersion") != args.darwin_state_version or current.get("checkout") != str(args.checkout):
        helper.fail("system", "private patch conflicts with explicit compatibility integer or checkout")
    projected = contract.project(result, "darwin")
    user = projected["profiles"]["darwin"]["user"]
    if user["signCommits"] and user["gitSigningKey"] is None:
        helper.fail("user.gitSigningKey", "missing actual signing key; supply it in the existing metadata or explicit private patch")
    result["profiles"]["darwin"] = projected["profiles"]["darwin"]
    helper.local_file_payloads(result)
    helper.backrest_payload(result)
    return result


def main():
    os.umask(0o077)
    args = arguments()
    helper_path = os.environ.get("PROFILE_HELPER_PYTHON")
    if not helper_path:
        print("convert-profile requires the packaged interpreter: bash tools/profile/convert-profile.sh or profile python", file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location("profile_helper", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    try:
        args.state_dir = args.state_dir.expanduser().absolute()
        if args.state_dir.resolve().is_relative_to(args.checkout.resolve()):
            raise helper.ProfileError("Private state must be outside the public checkout")
        source = helper.parse_json(helper.read_file(args.source, "source profile metadata"), "source profile metadata")
        if not isinstance(source, dict):
            helper.fail("profiles", "expected the existing profile document")
        result = convert(source, args, helper)
        if args.output:
            output = args.output.expanduser().absolute()
            if not output.resolve().is_relative_to(args.state_dir.resolve()):
                raise helper.ProfileError("Output must remain inside the private state directory")
            if output.resolve() in [args.source.resolve(), (args.state_dir / "profile.json").resolve()]:
                raise helper.ProfileError("Output must not overwrite active or source metadata")
            helper.atomic_file(output, helper.json_bytes(result), exclusive=True)
            print(output)
            return 0
        helper.secure_directory(args.state_dir)
        source_ciphertext = helper.read_file(args.state_dir / "profile.sops.yaml", "existing Darwin SOPS profile")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = args.state_dir / "backups" / ("profile." + stamp + "." + uuid.uuid4().hex + ".sops.yaml")
        helper.atomic_file(backup, source_ciphertext, exclusive=True)
        fd, temporary = tempfile.mkstemp(prefix=".conversion-", suffix=".json", dir=args.state_dir)
        candidate = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(helper.json_bytes(result))
            common = ["--platform", "darwin", "--state-dir", str(args.state_dir)]
            if args.backrest_config:
                common += ["--backrest-config", str(args.backrest_config)]
            if args.identity:
                common += ["--identity", str(args.identity)]
            subprocess.run([sys.executable, helper_path, "seal", *common, "--bootstrap", str(candidate)], check=True)
            subprocess.run([sys.executable, helper_path, "render", *common], check=True)
        finally:
            candidate.unlink(missing_ok=True)
        return 0
    except (helper.ProfileError, OSError, UnicodeError, subprocess.CalledProcessError, RecursionError) as error:
        print(str(error) if isinstance(error, helper.ProfileError) else "Metadata conversion failed; private data omitted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
