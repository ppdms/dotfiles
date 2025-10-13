#!/usr/bin/env bash
# secrets-edit-backup.sh
# Edit secrets.yaml with SOPS, then rekey and backup with passphrase-protected age key

set -euo pipefail

# Paths
SECRETS_YAML="$HOME/.config/nix/private/secrets/secrets.yaml"
BACKUP_KEY="$HOME/.config/nix/private/age/backup-key.txt"
BACKUP_PASSPHRASE_SECRET="backup_age_passphrase"
BACKUP_DIR="$HOME/.config/nix/private/secrets/backups"

mkdir -p "$BACKUP_DIR"

# 1. Decrypt backup passphrase from secrets.yaml using SOPS (with Secure Enclave)
BACKUP_PASSPHRASE=$(sops -d --extract "[\"$BACKUP_PASSPHRASE_SECRET\"]" "$SECRETS_YAML")

# 2. Export passphrase to env for age
export AGE_PASS="$BACKUP_PASSPHRASE"

# 3. Edit secrets.yaml with SOPS
sops "$SECRETS_YAML"

# 4. Rekey secrets.yaml to include both Secure Enclave and backup key
# (Assumes .sops.yaml is configured with both keys)
sops updatekeys "$SECRETS_YAML"

# 5. Backup secrets.yaml encrypted with backup key only
age --encrypt -r $(grep -m1 public "$BACKUP_KEY" | awk '{print $3}') "$SECRETS_YAML" > "$BACKUP_DIR/secrets.yaml.age.$(date +%Y%m%d%H%M%S)"

echo "Backup complete: $BACKUP_DIR"
