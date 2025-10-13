#!/usr/bin/env bash
# Helper script to remove passphrase from SSH key before storing in SOPS

set -e

if [ $# -ne 1 ]; then
    echo "Usage: $0 <secret_name>"
    echo "Example: $0 ssh_atansiblexh01"
    exit 1
fi

SECRET_NAME="$1"
SECRETS_FILE="$HOME/.config/nix/private/secrets/secrets.yaml"
TEMP_DIR=$(mktemp -d -t remove-passphrase.XXXXXX)
chmod 700 "$TEMP_DIR"

cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

echo "Extracting key from SOPS..."
if ! sops -d --extract "[\"$SECRET_NAME\"]" "$SECRETS_FILE" > "$TEMP_DIR/key_with_passphrase"; then
    echo "Failed to extract key from SOPS"
    exit 1
fi

chmod 600 "$TEMP_DIR/key_with_passphrase"

echo "Removing passphrase from key..."
echo "You will be prompted for the current passphrase, then press Enter twice for no new passphrase:"
ssh-keygen -p -f "$TEMP_DIR/key_with_passphrase" -N ""

if [ $? -eq 0 ]; then
    echo ""
    echo "Passphrase removed successfully!"
    echo ""
    echo "Now update SOPS with the new key:"
    echo "  sops ~/.config/nix/private/secrets/secrets.yaml"
    echo ""
    echo "Replace the '$SECRET_NAME' value with:"
    cat "$TEMP_DIR/key_with_passphrase"
    echo ""
    echo "Or copy to clipboard:"
    echo "  cat $TEMP_DIR/key_with_passphrase | pbcopy"
else
    echo "Failed to remove passphrase"
    exit 1
fi
