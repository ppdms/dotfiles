import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
from principals import manifest, principal, require_admin, run_as, tool

def rpc(method, payload):
    request = urllib.request.Request(
        f"http://127.0.0.1:{manifest()['ports']['backrest']}/v1.Backrest/" + method,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def restic(uri, *args):
    result = run_as("backrest",
        [tool("podman"), "exec", "backrest", "/bin/restic", "--no-lock", "--repo", uri, *args],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=300,
    )
    if result.returncode:
        raise RuntimeError("Repository access failed; verify the configured password, GDrive account and destination. No initialization or unlock attempted.")
    return json.loads(result.stdout)


def main():
    if os.getuid() != principal("backrest")["uid"]:
        require_admin()
    os.umask(0o077)
    # Required non-secret metadata; credentials stay in the scoped broker.
    uri = os.environ["HOMELAB_REPOSITORY_URI"]
    repo_id = os.environ["HOMELAB_REPOSITORY_ID"]
    if not uri or not repo_id:
        raise ValueError("HOMELAB_REPOSITORY_URI and HOMELAB_REPOSITORY_ID must not be empty")
    for component in ("backrest",):
        run_as("backrest", [os.environ["HOMELAB_SECRETS"], "status", component], check=True, timeout=60)
    configuration = rpc("GetConfig", {})
    repository = restic(uri, "cat", "config")
    guid = repository["id"]
    if len(guid) != 64 or any(c not in "0123456789abcdef" for c in guid):
        raise RuntimeError("Repository returned an invalid identity")
    snapshots = restic(uri, "snapshots", "--json")
    existing = [repo for repo in configuration.get("repos", []) if repo.get("id") == repo_id]
    if existing:
        if existing[0].get("guid") != guid or existing[0].get("uri") != uri:
            raise RuntimeError("Configured Backrest repository selection differs from the requested identity or URI; refusing to overwrite it")
    else:
        rpc("AddRepo", {"repo": {
            "id": repo_id, "uri": uri, "guid": guid, "password": "",
            "env": ["RESTIC_PASSWORD_FILE=/run/secrets/restic-password", "RCLONE_CONFIG=/run/rclone/rclone.conf"],
            "autoInitialize": False, "autoUnlock": False,
            "prunePolicy": {"schedule": {"disabled": True}},
            "checkPolicy": {"schedule": {"disabled": True}}, "hooks": [],
        }})
    inventory = Path(principal("backrest")["stateRoot"]) / "backrest/snapshot-inventory.json"
    data = json.dumps({"repository": uri, "guid": guid, "snapshots": snapshots}, indent=2) + "\n"
    script = (
        "import os,pathlib,sys,tempfile\n"
        "p=pathlib.Path(sys.argv[1])\n"
        "with tempfile.NamedTemporaryFile(mode='w',dir=p.parent,prefix='.inventory-',delete=False) as f:\n"
        " f.write(sys.stdin.read()); f.flush(); os.fchmod(f.fileno(),0o600); name=f.name\n"
        "os.replace(name,p)\n"
    )
    run_as("backrest", [tool("python"), "-c", script, str(inventory)], input=data, text=True, check=True, timeout=60)
    rpc("DoRepoTask", {"repoId": repo_id, "task": "TASK_INDEX_SNAPSHOTS"})
    run_as("backrest", [os.environ["HOMELAB_SECRETS"], "persist", "backrest"], check=True, timeout=60)
    print("Repository identity: " + guid)
    print("Unfiltered snapshot inventory: " + str(inventory))
    print("Backrest Index Snapshots queued; inspect its operation result in the UI.")
    for snapshot in snapshots:
        print(json.dumps({key: snapshot.get(key) for key in ("id", "time", "hostname", "tags", "paths")}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("Repository discovery failed; Backrest must be running with the required encrypted credentials available. No initialization, prune or unlock was attempted.", file=sys.stderr)
        sys.exit(1)
