#!/usr/bin/env python3
"""Prepare private Linux deployment inputs without activating or publishing."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from urllib.parse import quote


def render_deployment_flake(boilerplate_source, profile_input):
    """Canonical private wrapper composition, also usable by bootstrap fixtures."""
    boilerplate_url = "path:" + quote(str(boilerplate_source), safe="/")
    profile_url = "path:" + quote(str(profile_input), safe="/")
    return (
        "{\n"
        '  description = "Private local server deployment";\n'
        "  inputs.boilerplate.url = " + json.dumps(boilerplate_url) + ";\n"
        "  inputs.profile.url = " + json.dumps(profile_url) + ";\n"
        "  outputs = { boilerplate, profile, ... }:\n"
        "    boilerplate.lib.machines profile;\n"
        "}\n"
    ).encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("NIX_CONFIG_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "nix-config")))
    parser.add_argument("--update", action="store_true", help="Select latest approved local image-feed HEAD and update public dependencies; omitted for unattended preparation")
    args = parser.parse_args()
    os.umask(0o077)
    helper_path = os.environ.get("PROFILE_HELPER_PYTHON")
    if not helper_path:
        print("prepare-deployment: use tools/profile/prepare-deployment.sh or packaged profile python", file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location("profile_helper", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    try:
        state = args.state_dir.expanduser().absolute()
        args.state_dir = state
        deployment = state / "deployment"
        lock = deployment / "flake.lock"
        if deployment.is_symlink() or (deployment.exists() and not deployment.is_dir()):
            raise helper.ProfileError("Unsafe private deployment directory")
        if lock.is_symlink() or (lock.exists() and not lock.is_file()):
            raise helper.ProfileError("Refusing an unsafe private deployment lock")
        common = ["--platform", "nixos", "--state-dir", str(state)]
        def profile(*arguments):
            subprocess.run([sys.executable, helper_path, *arguments], check=True)
        profile("render", *common)
        source = helper.parse_json(helper.read_file(state / "profile.json", "synchronized private metadata"), "synchronized private metadata")
        contract = helper.Contract()
        selected = contract.project(source, "nixos")["profiles"]["nixos"]
        checkout = Path(selected["system"]["checkout"]).resolve()
        if not checkout.is_dir() or not (checkout / "flake.nix").is_file():
            raise helper.ProfileError("Selected checkout has no public root flake")
        root = helper.command(["git", "-C", str(checkout), "rev-parse", "--show-toplevel"],
                              "public Git checkout selection").decode().strip()
        if Path(root).resolve() != checkout:
            raise helper.ProfileError("Public source must be a Git checkout root")
        helper.command(["git", "-C", str(checkout), "rev-parse", "--verify", "HEAD^{commit}"],
                       "committed public Git source selection")
        public_url = "git+file://" + quote(str(checkout), safe="/")
        if state.resolve().is_relative_to(checkout.resolve()):
            raise helper.ProfileError("Private state must remain outside the public checkout")
        feed = contract.definition("imageFeed", source.get("imageFeed"))
        helper.secure_directory(deployment)
        if args.update:
            repository = Path(feed["path"])
            if repository.is_symlink() or not repository.is_dir():
                raise helper.ProfileError("Missing or unsafe approved local image feed")
            # The managed feed producer applies the seven-day observation
            # policy before committing pins. Consume that authority locally;
            # do not poll registries, fetch remote refs, or guess image ages.
            dirty = helper.command(["git", "--no-replace-objects", "-C", str(repository), "status", "--porcelain"], "approved image-feed cleanliness")
            if dirty.strip():
                raise helper.ProfileError("Approved local image feed has uncommitted changes")
            revision = helper.command(["git", "--no-replace-objects", "-C", str(repository), "rev-parse", "--verify", "HEAD^{commit}"], "approved image-feed HEAD selection").decode().strip()
            candidate = dict(source)
            candidate["imageFeed"] = {"path": str(repository), "revision": revision}
            helper.image_pins(contract, candidate)
            if revision != feed["revision"]:
                profile("update-images", *common, "--image-feed", str(repository), "--revision", revision)
            # Public dependencies are only updated by the explicit manual
            # --update operation. No private override accompanies this call.
            subprocess.run(["nix", "flake", "update", "--flake", public_url], check=True)
        # The path fetcher needs the physical immutable generation, not the
        # controlled STATE/input symlink. Never point it at surrounding state.
        profile_input = helper.current_input(args)
        if profile_input is None:
            raise helper.ProfileError("Missing immutable rendered profile input")
        # Let Nix archive the Git-filtered working tree. This includes tracked
        # review changes without importing ignored/private files or .git, and
        # yields a lockable immutable source even before the review is committed.
        metadata = helper.parse_json(
            helper.command(["nix", "flake", "metadata", "--json", "--no-write-lock-file",
                            public_url],
                           "Git-filtered public-source snapshot"),
            "public-source metadata")
        source_path = metadata.get("path") if isinstance(metadata, dict) else None
        if not isinstance(source_path, str) or not source_path.startswith("/nix/store/"):
            raise helper.ProfileError("Public source snapshot is not an immutable store path")
        boilerplate_source = Path(source_path)
        if not boilerplate_source.is_dir() or not (boilerplate_source / "flake.nix").is_file():
            raise helper.ProfileError("Public source snapshot has no root flake")
        # Flake input URLs must be literal values, not JSON-read thunks.
        # URI encoding also prevents Nix string interpolation in path values.
        helper.atomic_file(deployment / "flake.nix",
                           render_deployment_flake(boilerplate_source, profile_input))
        # Refresh only top-level local refs; their public lock governs the
        # transitive dependency graph. This lock is private, unlike root's.
        subprocess.run(["nix", "flake", "update", "boilerplate", "profile", "--flake", "path:" + str(deployment)], check=True)
        info = lock.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise helper.ProfileError("Private deployment did not produce a regular lock")
        os.chmod(lock, 0o600)
        return 0
    except (helper.ProfileError, OSError, ValueError, subprocess.CalledProcessError, RecursionError) as error:
        print(str(error) if isinstance(error, helper.ProfileError) else "Deployment preparation failed; private metadata omitted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
