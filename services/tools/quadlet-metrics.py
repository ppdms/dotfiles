"""Emit per-quadlet cgroup metrics in Prometheus textfile format.

Reads memory.current + cpu.stat from each unit cgroup under the user
app.slice, maps container names to systemd units via `podman ps`, and
writes homelab_quadlet_* metrics for the dashboard's per-quadlet panels
plus stack grouping.

Container name -> stack mapping lives here (single source); keep in sync
with the Alloy journal stack map in config/alloy/config.alloy.
"""
import json
import os

import tempfile
from pathlib import Path
from principals import current_principal, manifest, principal, run_as, tool


CONTAINER_STACK = {
    "alloy": "alloy",
    "authelia": "authelia",
    "backrest": "backrest",
    "cloudflared": "cloudflared",
    "esphome": "esphome",
    "homeassistant": "homeassistant",
    "immich_postgres": "immich",
    "immich_redis": "immich",
    "immich_server": "immich",
    "immich_microservices": "immich",
    "immich_machine_learning": "immich-ml",
    "jellyfin": "jellyfin",
    "nginx-proxy": "nginx-proxy",
    "opencloud": "opencloud",
    "termix": "termix",
    "TransmissionVPN": "transmission",
    "TransmissionVPN-Gluetun": "transmission",
    "kavita": "kavita",
    "tree-eclass-dev": "tree-eclass",
    "tree-eclass": "tree-eclass",
    "silverbullet": "silverbullet",
    "speedtest": "speedtest",
    "readeck": "readeck",
}


def podman_units(role: str) -> dict[str, str]:
    """Container ID prefix -> systemd unit from PODMAN_SYSTEMD_UNIT label."""
    try:
        out = run_as(role,
            [tool("podman"), "ps", "--format", "{{.ID}} {{.Names}}"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return {}
    result = {}
    for line in out.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        cid, name = parts
        try:
            d = json.loads(run_as(role,
                [tool("podman"), "inspect", cid, "--format", "json"],
                capture_output=True, text=True, timeout=30,
            ).stdout)[0]
            unit = d.get("Config", {}).get("Labels", {}).get("PODMAN_SYSTEMD_UNIT", "")
            if unit:
                result[name] = unit
        except Exception:
            pass
    return result


def read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip().split()[0])
    except Exception:
        return None


def read_cpu(path: Path) -> int | None:
    try:
        for line in path.read_text().splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1])
    except Exception:
        pass
    return None


def main() -> int:
    role = current_principal()
    identity = principal(role)
    units = podman_units(role)
    slice_path = Path(f"/sys/fs/cgroup/user.slice/user-{identity['uid']}.slice/user@{identity['uid']}.service/app.slice")
    lines = [
        "# HELP homelab_quadlet_memory_bytes Current cgroup memory usage per quadlet unit.",
        "# TYPE homelab_quadlet_memory_bytes gauge",
    ]
    cpu_lines = [
        "# HELP homelab_quadlet_cpu_seconds_total Cumulative cgroup CPU seconds per quadlet unit.",
        "# TYPE homelab_quadlet_cpu_seconds_total counter",
    ]
    count = 0
    for unit_dir in sorted(slice_path.glob("*.service")):
        unit = unit_dir.name
        if unit not in identity["units"] or unit.endswith("-migrate.service"):
            continue
        mem = read_int(unit_dir / "memory.current")
        cpu_usec = read_cpu(unit_dir / "cpu.stat")
        # Prefer container name for stack; fall back to unit stem.
        names = [n for n, u in units.items() if u == unit]
        name = names[0] if names else unit.removesuffix(".service")
        stack = CONTAINER_STACK.get(name, unit.removesuffix(".service"))
        if mem is not None:
            lines.append(
                f'homelab_quadlet_memory_bytes{{unit="{unit}",stack="{stack}"}} {mem}'
            )
            count += 1
        if cpu_usec is not None:
            cpu_lines.append(
                f'homelab_quadlet_cpu_seconds_total{{unit="{unit}",stack="{stack}"}} {cpu_usec / 1e6:.6f}'
            )
    out_dir = Path(manifest()["metricsDirectory"]) / role
    if out_dir.is_symlink() or out_dir.stat().st_uid != os.getuid():
        raise RuntimeError("metrics output directory is not owned by this principal")
    with tempfile.NamedTemporaryFile(mode="w", prefix=".quadlet-", dir=out_dir, delete=False) as output:
        tmp = Path(output.name)
        try:
            output.write("\n".join(lines + [""] + cpu_lines) + "\n")
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            tmp.replace(out_dir / "quadlet.prom")
        finally:
            tmp.unlink(missing_ok=True)
    print(f"wrote {count} quadlet series")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
