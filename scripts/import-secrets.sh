#!/usr/bin/env bash
#
# Import Secrets from Backup
# Restores secrets from an exported backup bundle
#
# Usage: ./import-secrets.sh <backup-file.tar.gz.age>

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

if [ $# -lt 1 ]; then
    echo -e "${RED}Error: No backup file specified${NC}"
    echo "Usage: $0 <backup-file.tar.gz.age>"
    exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "$BACKUP_FILE" ]; then
    echo -e "${RED}Error: Backup file not found: $BACKUP_FILE${NC}"
    exit 1
fi

echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  Import Secrets from Backup${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo

# Check dependencies
if ! command -v age &> /dev/null; then
    echo -e "${RED}Error: age is not installed${NC}"
    echo "Install with: nix-shell -p age"
    exit 1
fi

CONFIG_DIR="$HOME/.config/nix"
PRIVATE_DIR="$CONFIG_DIR/private"
AGE_DIR="$PRIVATE_DIR/age"
SECRETS_DIR="$PRIVATE_DIR/secrets"

# Create directories
mkdir -p "$AGE_DIR" "$SECRETS_DIR"

# Create temporary directory
TEMP_DIR=$(mktemp -d)
trap "rm -rf '$TEMP_DIR'" EXIT

echo -e "${BLUE}1. Decrypting backup bundle...${NC}"
echo -e "${YELLOW}Enter backup key passphrase:${NC}"

# First, we need to get the backup key from the bundle
# The bundle is encrypted with the backup key, but contains the backup key
# This is a chicken-and-egg problem. Let's handle it:

if [ -f "$AGE_DIR/backup-keys.txt" ]; then
    echo -e "${YELLOW}Using existing backup key from: $AGE_DIR/backup-keys.txt${NC}"
    SOPS_AGE_KEY_FILE="$AGE_DIR/backup-keys.txt" \
        age -d -i "$AGE_DIR/backup-keys.txt" "$BACKUP_FILE" | tar -xzf - -C "$TEMP_DIR"
else
    echo -e "${YELLOW}Backup key not found locally.${NC}"
    echo -e "${YELLOW}If you have the encrypted backup key (backup-keys.txt.age), place it at:${NC}"
    echo -e "  ${BLUE}$AGE_DIR/backup-keys.txt.age${NC}"
    echo
    read -p "Do you have the encrypted backup key available? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        if [ -f "$AGE_DIR/backup-keys.txt.age" ]; then
            echo -e "${BLUE}Decrypting backup key...${NC}"
            age -d "$AGE_DIR/backup-keys.txt.age" > "$AGE_DIR/backup-keys.txt.tmp"
            age -d -i "$AGE_DIR/backup-keys.txt.tmp" "$BACKUP_FILE" | tar -xzf - -C "$TEMP_DIR"
            mv "$AGE_DIR/backup-keys.txt.tmp" "$AGE_DIR/backup-keys.txt"
        else
            echo -e "${RED}Error: backup-keys.txt.age not found at $AGE_DIR/backup-keys.txt.age${NC}"
            exit 1
        fi
    else
        echo -e "${RED}Cannot proceed without backup key${NC}"
        exit 1
    fi
fi

echo -e "${GREEN}✓ Bundle decrypted${NC}"

# Check if extraction was successful
EXPORT_DIR="$TEMP_DIR/nix-secrets-export"
if [ ! -d "$EXPORT_DIR" ]; then
    echo -e "${RED}Error: Invalid backup bundle format${NC}"
    exit 1
fi

# Copy files
echo -e "${BLUE}2. Copying secrets and configuration...${NC}"

if [ ! -f "$AGE_DIR/backup-keys.txt" ]; then
    cp "$EXPORT_DIR/backup-keys.txt" "$AGE_DIR/"
    chmod 600 "$AGE_DIR/backup-keys.txt"
    echo -e "${GREEN}✓ Backup key installed${NC}"
else
    echo -e "${YELLOW}⚠ Backup key already exists, skipping${NC}"
fi

cp "$EXPORT_DIR/secrets.yaml" "$SECRETS_DIR/"
cp "$EXPORT_DIR/.sops.yaml" "$SECRETS_DIR/"
echo -e "${GREEN}✓ Secrets and configuration copied${NC}"

# Test decryption
echo -e "${BLUE}3. Verifying secrets can be decrypted...${NC}"
if SOPS_AGE_KEY_FILE="$AGE_DIR/backup-keys.txt" sops -d "$SECRETS_DIR/secrets.yaml" > /dev/null; then
    echo -e "${GREEN}✓ Secrets verified successfully${NC}"
else
    echo -e "${RED}✗ Failed to decrypt secrets${NC}"
    exit 1
fi

echo
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Import Complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo
echo -e "${BLUE}Imported files:${NC}"
echo "  • $AGE_DIR/backup-keys.txt"
echo "  • $SECRETS_DIR/secrets.yaml"
echo "  • $SECRETS_DIR/.sops.yaml"
echo
echo -e "${BLUE}Next steps:${NC}"
echo
echo "1. ${YELLOW}Test decryption:${NC}"
echo "   SOPS_AGE_KEY_FILE=$AGE_DIR/backup-keys.txt \\"
echo "     sops -d $SECRETS_DIR/secrets.yaml"
echo
echo "2. ${YELLOW}(Optional) Set up Secure Enclave key for this machine:${NC}"
echo "   age-plugin-se -o $AGE_DIR/keys.txt"
echo
echo "3. ${YELLOW}(Optional) Add both keys to SOPS and re-key:${NC}"
echo "   cd $(dirname "$0")"
echo "   ./rekey-with-both.sh"
echo
echo "4. ${YELLOW}(Recommended) Delete the backup file after verification:${NC}"
echo "   shred -u '$BACKUP_FILE' || rm -P '$BACKUP_FILE'"
echo
echo -e "${BLUE}View import instructions:${NC}"
echo "  cat $EXPORT_DIR/README.txt"
echo
