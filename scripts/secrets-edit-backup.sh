#!/usr/bin/env bash
# secrets-edit-backup.sh
# Edit secrets.yaml with SOPS, then rekey with both keys and create backup

set -euo pipefail

# Paths
SECRETS_YAML="$HOME/.config/nix/private/secrets/secrets.yaml"
BACKUP_KEY_SECRET="backup_age_key"
BACKUP_DIR="$HOME/.config/nix/private/secrets/backups"
TEMP_KEY="$(mktemp)"

mkdir -p "$BACKUP_DIR"

# Function to cleanup temp files
cleanup() {
    rm -f "$TEMP_KEY"
}
trap cleanup EXIT

# 1. Extract the backup age key from secrets.yaml (decrypt with Secure Enclave)
echo "Extracting backup key..."
sops -d --extract "[\"$BACKUP_KEY_SECRET\"]" "$SECRETS_YAML" > "$TEMP_KEY"

# Extract public key from the private key
BACKUP_PUBLIC_KEY=$(age-keygen -y "$TEMP_KEY")
echo "Backup key public: $BACKUP_PUBLIC_KEY"

# 2. Edit secrets.yaml with SOPS
sops "$SECRETS_YAML"
EDIT_EXIT_CODE=$?

# Check if file was actually edited (sops exits with 200 if no changes)
if [ $EDIT_EXIT_CODE -eq 200 ]; then
    echo "No changes made to secrets.yaml"
    exit 0
elif [ $EDIT_EXIT_CODE -ne 0 ]; then
    echo "Error editing secrets.yaml (exit code: $EDIT_EXIT_CODE)"
    exit $EDIT_EXIT_CODE
fi

# 3. Rekey secrets.yaml to include both Secure Enclave and backup key
# (Assumes .sops.yaml is configured with both keys)
echo "Rekeying with both keys..."
cd "$(dirname "$SECRETS_YAML")"
sops updatekeys secrets.yaml

# 4. Create a backup: decrypt with SOPS, then encrypt with backup key only
TIMESTAMP=$(date +%Y%m%d%H%M%S)
BACKUP_FILE="$BACKUP_DIR/secrets.yaml.$TIMESTAMP.age"
echo "Creating backup..."
# Decrypt the SOPS-encrypted file, then encrypt the plaintext with age
sops -d "$SECRETS_YAML" | age --encrypt -r "$BACKUP_PUBLIC_KEY" > "$BACKUP_FILE"

echo "✓ Secrets edited and rekeyed with both keys"
echo "✓ Backup saved: $BACKUP_FILE"
echo "  (Plaintext encrypted with backup key only)"
