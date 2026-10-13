#!/usr/bin/env zsh

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.nix-profile/bin:/etc/profiles/per-user/${USER}/bin:/run/current-system/sw/bin:/nix/var/nix/profiles/default/bin:$PATH"

usage() {
  cat <<'EOF'
Usage: recreate-darwin-from-backup.zsh --backup-file PATH [options]

Options:
  --nix-dir PATH      Bootstrap repository root (default: this script's checkout)
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

nix_dir="$(CDPATH= cd -P -- "$(dirname -- "$0")/../.." && pwd -P)"
state_dir="${NIX_CONFIG_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/nix-config}"
backup_key="${NIX_CONFIG_INNER_IDENTITY:-${XDG_CONFIG_HOME:-$HOME/.config}/sops/age/outer-key.txt}"
backup_file=""
host=""
skip_update=0
no_switch=0

while (( $# > 0 )); do
  case "$1" in
    --nix-dir|--state-dir|--backup-key|--backup-file|--host)
      (( $# >= 2 )) && [[ -n "$2" ]] || die "$1 requires a value" ;;
  esac
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
for input_path in \
  "$backup_file" \
  "$backup_key" \
  "$state_dir/.sops.yaml" \
  "$state_dir/profile.sops.yaml"; do
  [[ -f "$input_path" ]] || die "Missing local input: $input_path"
done

tmp_dir="$(mktemp -d)"
chmod 700 "$tmp_dir"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

age --decrypt -i "$backup_key" -o "$tmp_dir/secrets.yaml" "$backup_file"
if tar -tf "$tmp_dir/secrets.yaml" >/dev/null 2>&1; then
  mv "$tmp_dir/secrets.yaml" "$tmp_dir/secrets.bundle"
  tar -xOf "$tmp_dir/secrets.bundle" secrets.yaml > "$tmp_dir/secrets.yaml"
  if tar -xOf "$tmp_dir/secrets.bundle" rclone.ini > "$tmp_dir/rclone.ini" 2>/dev/null; then
    has_rclone=1
  else
    rm -f "$tmp_dir/rclone.ini"
    has_rclone=0
  fi
else
  has_rclone=0
fi
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
if (( has_rclone == 1 )); then
  sops \
    --encrypt \
    --input-type ini \
    --output-type ini \
    --filename-override rclone.ini \
    --config "$state_dir/.sops.yaml" \
    --output "$tmp_dir/rclone.sops.ini" \
    "$tmp_dir/rclone.ini"
  install -m 600 "$tmp_dir/rclone.sops.ini" "$state_dir/rclone.ini"
fi

export NIX_CONFIG_STATE_DIR="$state_dir"
export NIX_CONFIG_INNER_IDENTITY="$backup_key"
bash "$nix_dir/tools/profile/render-profile.sh" --platform darwin --state-dir "$state_dir" --force >/dev/null
if [[ -n "$host" ]]; then
  selected_host="$(bash -c 'source "$1"; shift; profile_exec "$@"' \
    profile-query "$nix_dir/tools/profile/profile-bootstrap.sh" \
    query --platform darwin --state-dir "$state_dir" --field hostname)"
  [[ "$host" == "$selected_host" ]] || die "--host must match the selected profile hostname"
fi
rebuild_args=(--platform darwin --state-dir "$state_dir")
(( skip_update == 1 )) || rebuild_args+=(--update)
(( no_switch == 0 )) || rebuild_args+=(--no-switch)
bash "$nix_dir/tools/profile/rebuild.sh" "${rebuild_args[@]}"
