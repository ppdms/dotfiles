"""Scrape the NixOS smartctl exporter (loopback :9633) into homelab_drive_*
textfile metrics for Alloy's textfile collector.

Maps smartctl_device_* (real exporter names) to the dashboard's
homelab_drive_* contract:
  temperature{temperature_type=current} -> temperature_celsius{device}
  available_spare -> available_spare_percent{device}
  percentage_used -> percentage_used{device}
  smart_status -> health_ok{device}
  media_errors -> media_errors_total{device}
  power_on_seconds -> power_on_hours_total{device} (/3600)
Runs only as the SMART metrics principal; output 0644 in its own directory.
"""
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path
from principals import current_principal, manifest

URL = "http://127.0.0.1:9633/metrics"

WANT = {
    "smartctl_device_temperature": ("homelab_drive_temperature_celsius", "gauge",
                                     {"temperature_type": "current"}),
    "smartctl_device_available_spare": ("homelab_drive_available_spare_percent", "gauge", {}),
    "smartctl_device_percentage_used": ("homelab_drive_percentage_used", "gauge", {}),
    "smartctl_device_smart_status": ("homelab_drive_health_ok", "gauge", {}),
    "smartctl_device_media_errors": ("homelab_drive_media_errors_total", "counter", {}),
    "smartctl_device_power_on_seconds": ("homelab_drive_power_on_hours_total", "counter", {}),
}


def parse_labels(text: str) -> dict[str, str]:
    out = {}
    for part in re.findall(r'(\w+)="([^"]*)"', text):
        out[part[0]] = part[1]
    return out


def main() -> int:
    if current_principal() != "metrics":
        raise RuntimeError("SMART collection requires the metrics account")
    out_dir = Path(manifest()["metricsDirectory"]) / "metrics"
    if out_dir.is_symlink() or out_dir.stat().st_uid != os.getuid():
        raise RuntimeError("metrics output directory is not owned by this principal")
    try:
        with urllib.request.urlopen(URL, timeout=20) as response:
            body = response.read().decode()
    except Exception as error:
        print(f"smartctl exporter unreachable: {error}", file=sys.stderr)
        return 1
    series: dict[str, list[str]] = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(\w+)\{([^}]*)\}\s+(\S+)", line)
        if not match:
            continue
        name, label_text, value = match.groups()
        if name not in WANT:
            continue
        out_name, kind, required = WANT[name]
        labels = parse_labels(label_text)
        if any(labels.get(k) != v for k, v in required.items()):
            continue
        device = labels.get("device", "unknown")
        try:
            number = float(value)
        except ValueError:
            continue
        if name == "smartctl_device_power_on_seconds":
            number /= 3600.0
        series.setdefault(out_name, []).append(
            f'{out_name}{{device="{device}"}} {number:g}'
        )
    if not series:
        print("no SMART series parsed", file=sys.stderr)
        return 1
    lines = []
    for out_name in sorted(series):
        _, kind, _ = WANT[
            next(k for k, v in WANT.items() if v[0] == out_name)
        ]
        lines.append(f"# HELP {out_name} SMART drive health bridged from smartctl exporter.")
        lines.append(f"# TYPE {out_name} {kind}")
        lines.extend(sorted(series[out_name]))
    with tempfile.NamedTemporaryFile(mode="w", prefix=".smart-", dir=out_dir, delete=False) as output:
        tmp = Path(output.name)
        try:
            output.write("\n".join(lines) + "\n")
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            tmp.replace(out_dir / "smart.prom")
        finally:
            tmp.unlink(missing_ok=True)
    print(f"wrote {sum(len(v) for v in series.values())} drive series")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
