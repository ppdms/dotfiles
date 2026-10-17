#!/usr/bin/env bash
# Install a verified SilverBullet client executable for the homelab-silverbullet
# account and restart only that account's SilverBullet service. Build the
# executable first (see docs/silverbullet.md); this does not touch the image,
# the notes, or any other service.
set -euo pipefail

if [[ ${1:-} == --help || $# != 1 ]]; then
  printf 'Usage: sudo bash %s /absolute/path/to/verified/silverbullet\n' "$0"
  [[ ${1:-} == --help ]] && exit 0
  exit 2
fi
if [[ $EUID != 0 ]]; then
  echo "Deployment requires sudo for the homelab-silverbullet account." >&2
  exit 1
fi

candidate=$(realpath "$1")
[[ -x "$candidate" ]] || { echo "Verified executable is missing: $candidate" >&2; exit 1; }
account=homelab-silverbullet
IFS=: read -r _ _ uid gid _ home _ < <(getent passwd "$account")
[[ -n ${uid:-} && -n ${home:-} ]] || { echo "Missing dedicated account: $account" >&2; exit 1; }
binary="$home/state/silverbullet/editor-modes/silverbullet"
override="$home/.config/containers/systemd/silverbullet.container.d/10-editor-modes.conf"
template=$(mktemp)
trap 'rm -f "$template"' EXIT
# Must match the override declared in services/silverbullet.nix.
printf '[Container]\nVolume=%s:/silverbullet:ro\n' "$binary" > "$template"

as_service() {
  runuser -u "$account" -- env -i \
    HOME="$home" USER="$account" LOGNAME="$account" \
    XDG_RUNTIME_DIR="/run/user/$uid" \
    PATH=/run/wrappers/bin:/run/current-system/sw/bin \
    "$@"
}

# Only this feature's override is owned here. Never replace a differing file.
if [[ -e "$override" || -L "$override" ]] && ! cmp -s "$template" "$override"; then
  echo "Existing override differs; review it before deployment: $override" >&2
  exit 1
fi
as_service systemctl --user cat silverbullet.service > /dev/null

install -d -o "$uid" -g "$gid" -m 0700 "$(dirname "$binary")" "$(dirname "$override")"
install -o "$uid" -g "$gid" -m 0755 "$candidate" "$binary.next"
mv -f "$binary.next" "$binary"
if [[ ! -e "$override" ]]; then
  install -o "$uid" -g "$gid" -m 0644 "$template" "$override.next"
  mv -f "$override.next" "$override"
fi
as_service systemctl --user daemon-reload
as_service systemctl --user restart silverbullet.service
as_service systemctl --user is-active silverbullet.service
printf 'Deployed %s\nReload the browser to load the new client.\n' "$binary"
