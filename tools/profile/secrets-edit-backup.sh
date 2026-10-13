#!/usr/bin/env bash

set -euo pipefail
umask 077

if [[ $# -ne 0 ]]; then
    printf 'Usage: %s\n' "${0##*/}" >&2
    exit 2
fi

state_dir="${NIX_CONFIG_STATE_DIR:-${XDG_STATE_HOME:-"$HOME/.local/state"}/nix-config}"
identity="${NIX_CONFIG_INNER_IDENTITY:-${XDG_CONFIG_HOME:-"$HOME/.config"}/sops/age/outer-key.txt}"
policy="$state_dir/.sops.yaml"
secrets="$state_dir/secrets.sops.yaml"
rclone="$state_dir/rclone.ini"
backup_dir="$state_dir/backups"
backup_key_name="backup_age_key"

tmp_key="$(mktemp)"
tmp_backup_dir="$(mktemp -d)"
cleanup() {
    rm -f "$tmp_key"
    rm -rf "$tmp_backup_dir"
}
trap cleanup EXIT

for path in "$identity" "$policy" "$secrets" "$rclone"; do
    [[ -f "$path" ]] || {
        printf 'Missing local input: %s\n' "$path" >&2
        exit 1
    }
done
mkdir -p "$backup_dir"

export SOPS_AGE_KEY_FILE="$identity"
sops --config "$policy" --decrypt --extract "[\"$backup_key_name\"]" "$secrets" > "$tmp_key"
backup_recipient="$(age-keygen -y "$tmp_key")"

set +e
sops --config "$policy" "$secrets"
sops_edit_status=$?
sops --config "$policy" --input-type ini "$rclone"
rclone_edit_status=$?
set -e

if [[ $sops_edit_status -ne 0 && $sops_edit_status -ne 200 ]]; then
    exit "$sops_edit_status"
elif [[ $rclone_edit_status -ne 0 && $rclone_edit_status -ne 200 ]]; then
    exit "$rclone_edit_status"
fi

if [[ $sops_edit_status -eq 200 && $rclone_edit_status -eq 200 ]]; then
    printf 'No changes made.\n'
    exit 0
fi

sops --config "$policy" updatekeys --yes "$secrets"
sops --config "$policy" --input-type ini updatekeys --yes "$rclone"
timestamp="$(date +%Y%m%d%H%M%S)"
sops --config "$policy" --decrypt "$secrets" > "$tmp_backup_dir/secrets.yaml"
sops --config "$policy" --input-type ini --decrypt "$rclone" > "$tmp_backup_dir/rclone.ini"
backup_file="$backup_dir/secrets.$timestamp.tar.age"
tar -C "$tmp_backup_dir" -cf - secrets.yaml rclone.ini |
    age --encrypt --recipient "$backup_recipient" --output "$backup_file"

printf 'Updated %s\n' "$secrets"
printf 'Updated %s\n' "$rclone"
printf 'Backup: %s\n' "$backup_file"
