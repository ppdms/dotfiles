#!/usr/bin/env zsh

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: recreate-darwin-from-backup.zsh --backup-file PATH [options]

Options:
  --nix-dir PATH      Repository root (default: ~/.config/nix)
  --state-dir PATH    Local profile state (default: ~/.local/state/nix-config)
  --backup-key PATH   Backup age identity (default: ~/.config/sops/age/outer-key.txt)
  --host NAME         Darwin configuration name (default: local profile)
  --skip-update       Skip nix flake update
  --no-switch         Restore only
  -h, --help          Show this help
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

nix_dir="$HOME/.config/nix"
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/nix-config"
backup_key="${XDG_CONFIG_HOME:-$HOME/.config}/sops/age/outer-key.txt"
backup_file=""
host=""
skip_update=0
no_switch=0

while (( $# > 0 )); do
  case "$1" in
    --nix-dir) nix_dir="$2"; shift 2 ;;
    --state-dir) state_dir="$2"; shift 2 ;;
    --backup-key) backup_key="$2"; shift 2 ;;
    --backup-file) backup_file="$2"; shift 2 ;;
    --host) host="$2"; shift 2 ;;
    --skip-update) skip_update=1; shift ;;
    --no-switch) no_switch=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "$backup_file" ]] || die "--backup-file is required"
for path in \
  "$backup_file" \
  "$backup_key" \
  "$state_dir/.sops.yaml" \
  "$state_dir/profile.sops.yaml"; do
  [[ -f "$path" ]] || die "Missing local input: $path"
done

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

age --decrypt -i "$backup_key" -o "$tmp_dir/secrets.yaml" "$backup_file"
export SOPS_AGE_KEY_FILE="$backup_key"
sops \
  --encrypt \
  --input-type yaml \
  --output-type yaml \
  --filename-override secrets.sops.yaml \
  --config "$state_dir/.sops.yaml" \
  --output "$tmp_dir/secrets.sops.yaml" \
  "$tmp_dir/secrets.yaml"
install -m 600 "$tmp_dir/secrets.sops.yaml" "$state_dir/secrets.sops.yaml"

NIX_CONFIG_STATE_DIR="$state_dir" \
NIX_CONFIG_INNER_IDENTITY="$backup_key" \
  "$nix_dir/scripts/render-profile.sh" --force >/dev/null

if [[ -z "$host" ]]; then
  host="$(jq -r '.profiles.darwin.system.hostname' "$state_dir/profile.json")"
fi
profile_url="path:$state_dir"
darwin_dir="$nix_dir/systems/darwin"

if (( skip_update == 0 )); then
  (cd "$darwin_dir" && nix flake update --override-input profile "$profile_url")
fi
if (( no_switch == 0 )); then
  sudo darwin-rebuild switch \
    --flake "$darwin_dir#$host" \
    --override-input profile "$profile_url"
fi
