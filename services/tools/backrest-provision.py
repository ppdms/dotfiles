import argparse
import configparser
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from homelab_secrets import private_read, load_field, load_record, store_fields, validate_fields
from principals import manifest, principal, require_admin, run_as, tool

SECRET_INPUTS = {
    "resticPasswordFile": ("backrest", "restic-password"),
    "rcloneConfigFile": ("backrest", "rclone.conf"),
    "authelia": {
        "usersFile": ("authelia", "users.yml"),
        "jwtSecretFile": ("authelia", "jwt-secret"),
        "sessionSecretFile": ("authelia", "session-secret"),
        "storageEncryptionKeyFile": ("authelia", "storage-encryption-key"),
    },
    "tls": {
        "certificateFile": ("nginx", "tls.crt"),
        "keyFile": ("nginx", "tls.key"),
        "publicCertificateFile": ("nginx", "tls-public.crt"),
        "publicKeyFile": ("nginx", "tls-public.key"),
    },
}


def input_path(value):
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("/nix/store/"):
        raise RuntimeError("Provision inputs must be absolute host file-path strings outside the Nix store")
    return Path(value)


def secret_inputs(recipe):
    allowed = set(SECRET_INPUTS)
    if set(recipe) - allowed:
        raise RuntimeError("Unknown provision input names")
    for name, spec in SECRET_INPUTS.items():
        supplied = recipe.get(name)
        if supplied is None:
            continue
        if isinstance(spec, tuple):
            yield (*spec, input_path(supplied))
        else:
            if not isinstance(supplied, dict) or set(supplied) - (set(spec) | ({"databaseFile"} if name == "authelia" else set())):
                raise RuntimeError("Unknown provision input group fields")
            for field, destination in spec.items():
                if supplied.get(field) is not None:
                    yield (*destination, input_path(supplied[field]))


def command(*args):
    subprocess.run(args, check=True)


def database_exists() -> bool:
    database = Path(principal("authelia")["stateRoot"]) / "authelia/data/db.sqlite3"
    result = run_as("authelia", [tool("python"), "-c",
                    "import pathlib,sys; p=pathlib.Path(sys.argv[1]); sys.exit(2 if p.is_symlink() or (p.exists() and not p.is_file()) else 0 if p.is_file() else 1)",
                    str(database)], capture_output=True, timeout=30)
    if result.returncode not in (0, 1):
        raise RuntimeError("Authelia identity database is invalid or its account is unavailable")
    return result.returncode == 0


def install_database(source: Path) -> None:
    """Stream a consistent SQLite backup into the Authelia service account."""
    database = Path(principal("authelia")["stateRoot"]) / "authelia/data/db.sqlite3"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(":memory:")) as snapshot:
        original.backup(snapshot)
        data = snapshot.serialize()
    script = (
        "import os,pathlib,sqlite3,sys,tempfile\nfrom contextlib import closing\n"
        "p=pathlib.Path(sys.argv[1]); data=sys.stdin.buffer.read()\n"
        "if p.is_symlink(): raise SystemExit('identity database must not be a symlink')\n"
        "if p.exists(): raise SystemExit(0)\n"
        "if p.parent.is_symlink(): raise SystemExit('invalid state directory')\n"
        "p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)\n"
        "with tempfile.NamedTemporaryFile(dir=p.parent,prefix='.database-',delete=False) as f: name=f.name\n"
        "try:\n"
        " with closing(sqlite3.connect(':memory:')) as src, closing(sqlite3.connect(name)) as dst:\n"
        "  src.deserialize(data); src.backup(dst)\n"
        "  if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('invalid database input')\n"
        " os.chmod(name,0o600); os.link(name,p)\n"
        "finally: os.unlink(name)\n"
    )
    run_as("authelia", [tool("python"), "-c", script, str(database)], input=data, check=True, timeout=120)


def main():
    parser = argparse.ArgumentParser(description="Provision the declarative Backrest recipe from private host files")
    parser.add_argument("--inputs", default=os.environ.get("HOMELAB_INPUTS"), help="JSON manifest containing file paths only, using the Nix provisionInputs schema")
    parser.add_argument("--check", action="store_true", help="Check input availability without importing or starting services")
    parser.add_argument("--no-start", action="store_true", help="Import missing inputs without replacing configured state, starting services or accessing the repository")
    args = parser.parse_args()
    require_admin()
    os.umask(0o077)
    recipe = json.loads(Path(args.inputs).read_text()) if args.inputs else {}
    if not isinstance(recipe, dict):
        raise RuntimeError("Provision input manifest must be an object containing file paths, never secret values")
    inputs = list(secret_inputs(recipe))
    database_source = (recipe.get("authelia") or {}).get("databaseFile")
    has_database = database_exists()
    # Validate every supplied new secret before importing any. Existing encrypted
    # inputs win: replaying a recipe must not roll back refreshed OAuth/users.
    for component, field, source in inputs:
        if load_field(component, field) is None:
            validate_fields(component, {field: private_read(source).decode("utf-8")}, complete=False)
    if database_source is not None:
        database_source = input_path(database_source)
        if not has_database:
            private_read(database_source)
            with closing(sqlite3.connect(database_source.as_uri() + "?mode=ro", uri=True)) as source:
                if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Authelia database input is not a consistent SQLite database")
    if args.check:
        missing = []
        supplied = {(component, field) for component, field, _ in inputs}
        for component, fields in (
            ("backrest", ("restic-password", "rclone.conf")),
            ("authelia", ("users.yml", "jwt-secret", "session-secret", "storage-encryption-key")),
            ("nginx", ("tls.crt", "tls.key")),
        ):
            for field in fields:
                if (component, field) not in supplied and load_field(component, field) is None:
                    missing.append(component + "/" + field)
        if not has_database and database_source is None:
            missing.append("authelia/databaseFile")
        if missing:
            print("Missing recipe inputs: " + ", ".join(missing), file=sys.stderr)
            return 1
        print("All provision input files or encrypted broker inputs are available; host ports/DNS/TLS authentication still require runtime verification.")
        return 0
    for component, field, source in inputs:
        owners = [name for name, role in manifest()["principals"].items() if component in role["components"]]
        for role in owners:
            run_as(role, [os.environ["HOMELAB_SECRETS"], "persist", component], check=True, timeout=60)
        fields, digest = load_record(component)
        if field not in fields:
            fields[field] = private_read(source).decode("utf-8")
            store_fields(component, fields, expected_digest=digest)
    if database_source is not None and not has_database:
        install_database(database_source)
        print("Imported a consistent Authelia database copy; source file unchanged.")
    if args.no_start:
        print("Inputs provisioned idempotently; configured encrypted inputs and application state unchanged.")
        return 0
    command(os.environ["HOMELAB_IMAGES"], "distribute", "--spec", os.environ["HOMELAB_IMAGE_SPEC"],
            "--signature-policy", os.environ["HOMELAB_SIGNATURE_POLICY"])
    for role in ("backrest", "authelia", "nginx"):
        run_as(role, [tool("systemctl"), "--user", "daemon-reload"], check=True, timeout=60)
        run_as(role, [tool("systemctl"), "--user", "reset-failed", *principal(role)["units"]], check=True, timeout=60)
    run_as("backrest", [tool("systemctl"), "--user", "restart", *principal("backrest")["units"]], check=True, timeout=120)
    command(os.environ["HOMELAB_REPOSITORY"])
    for role in ("authelia", "nginx"):
        run_as(role, [tool("systemctl"), "--user", "restart", *principal(role)["units"]], check=True, timeout=120)
    for role, identity in manifest()["principals"].items():
        if identity["module"] is not None:
            run_as(role, [tool("systemctl"), "--user", "start", "homelab.target"], check=True, timeout=180)
    print("Homelab services started. Verify the backup hostname redirects anonymous access to the authentication portal; select snapshot/path/destination before restoring.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, configparser.Error, sqlite3.Error, subprocess.SubprocessError):
        print("Provisioning blocked: check input names, private user-owned files, credential broker and service prerequisites. Existing inputs/state were not replaced; no repository initialization or destructive action attempted.", file=sys.stderr)
        sys.exit(1)
