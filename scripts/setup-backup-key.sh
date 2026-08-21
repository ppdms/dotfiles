#!/usr/bin/env bash

set -euo pipefail
umask 077

state_dir="${NIX_CONFIG_STATE_DIR:-${XDG_STATE_HOME:-"$HOME/.local/state"}/nix-config}"
age_dir="${XDG_CONFIG_HOME:-"$HOME/.config"}/sops/age"
secure_enclave_identity="${SOPS_SECURE_ENCLAVE_IDENTITY:-$age_dir/keys.txt}"
backup_identity="${SOPS_BACKUP_IDENTITY:-$age_dir/outer-key.txt}"
policy="$state_dir/.sops.yaml"

for command in age-keygen age-plugin-se sops; do
    command -v "$command" >/dev/null 2>&1 || {
        printf 'Missing command: %s\n' "$command" >&2
        exit 1
    }
done
for path in "$secure_enclave_identity" "$backup_identity"; do
    [[ -f "$path" ]] || {
        printf 'Missing local identity: %s\n' "$path" >&2
        exit 1
    }
done

mkdir -p "$state_dir"
secure_enclave_recipient="$(age-plugin-se recipients -i "$secure_enclave_identity" | head -n 1)"
backup_recipient="$(age-keygen -y "$backup_identity")"

cat > "$policy" <<EOF
keys:
  - &secure_enclave $secure_enclave_recipient
  - &backup $backup_recipient

creation_rules:
  - path_regex: .*\.yaml$
    key_groups:
      - age:
          - *secure_enclave
          - *backup
EOF
chmod 600 "$policy"

export SOPS_AGE_KEY_FILE="$backup_identity"
for file in "$state_dir/profile.sops.yaml" "$state_dir/secrets.sops.yaml"; do
    [[ -f "$file" ]] && sops --config "$policy" updatekeys --yes "$file"
done

printf 'Updated %s\n' "$policy"
