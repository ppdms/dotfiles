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
backup_dir="$state_dir/backups"
backup_key_name="backup_age_key"

tmp_key="$(mktemp)"
cleanup() {
    rm -f "$tmp_key"
}
trap cleanup EXIT

for path in "$identity" "$policy" "$secrets"; do
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
edit_status=$?
set -e

if [[ $edit_status -eq 200 ]]; then
    printf 'No changes made.\n'
    exit 0
elif [[ $edit_status -ne 0 ]]; then
    exit "$edit_status"
fi

sops --config "$policy" updatekeys --yes "$secrets"
timestamp="$(date +%Y%m%d%H%M%S)"
backup_file="$backup_dir/secrets.yaml.$timestamp.age"
sops --config "$policy" --decrypt "$secrets" |
    age --encrypt --recipient "$backup_recipient" --output "$backup_file"

printf 'Updated %s\n' "$secrets"
printf 'Backup: %s\n' "$backup_file"
