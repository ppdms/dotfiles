#!/usr/bin/env bash
#
# Setup Backup Key System
# This script creates a backup age key for SOPS secrets
# 
# Usage: ./setup-backup-key.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PRIVATE_DIR="$CONFIG_DIR/private"
AGE_DIR="$PRIVATE_DIR/age"
SECRETS_DIR="$PRIVATE_DIR/secrets"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  SOPS Backup Key Setup${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo

# Check if age is installed
if ! command -v age &> /dev/null; then
    echo -e "${RED}Error: age is not installed${NC}"
    echo "Install with: nix-shell -p age"
    exit 1
fi

# Check if sops is installed
if ! command -v sops &> /dev/null; then
    echo -e "${RED}Error: sops is not installed${NC}"
    echo "Install with: nix-shell -p sops"
    exit 1
fi

mkdir -p "$AGE_DIR"

BACKUP_KEY="$AGE_DIR/backup-key.txt"
SE_KEY="$AGE_DIR/keys.txt"

echo -e "${YELLOW}This will create a backup age key.${NC}"
echo -e "${YELLOW}This key can be used to decrypt secrets on any system.${NC}"
echo
echo -e "Location: ${GREEN}$BACKUP_KEY${NC}"
echo

# Check if backup key already exists
if [ -f "$BACKUP_KEY" ]; then
    echo -e "${YELLOW}Backup key already exists!${NC}"
    read -p "Do you want to recreate it? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
    # Remove old key
    rm -f "$BACKUP_KEY"
fi

# Generate the backup key
echo -e "${BLUE}Generating new age key...${NC}"
age-keygen -o "$BACKUP_KEY" 2>&1

# Extract the public key
BACKUP_PUBLIC_KEY=$(age-keygen -y "$BACKUP_KEY")
echo -e "${GREEN}✓ Generated backup key${NC}"
echo -e "  Public key: ${BLUE}$BACKUP_PUBLIC_KEY${NC}"
echo

# Get Secure Enclave public key
if [ -f "$SE_KEY" ]; then
    # Secure Enclave keys have public key in comments, not extractable via age-keygen -y
    SE_PUBLIC_KEY=$(grep "^# public key:" "$SE_KEY" | awk '{print $NF}')
    echo -e "${BLUE}Secure Enclave key detected:${NC}"
    echo -e "  Public key: ${BLUE}$SE_PUBLIC_KEY${NC}"
    echo
fi

# Update .sops.yaml
SOPS_CONFIG="$SECRETS_DIR/.sops.yaml"

echo -e "${BLUE}Updating SOPS configuration...${NC}"

cat > "$SOPS_CONFIG" << EOF
keys:
  - &secure_enclave $SE_PUBLIC_KEY
  - &backup $BACKUP_PUBLIC_KEY

creation_rules:
  - path_regex: .*\.yaml$
    key_groups:
      - age:
          - *secure_enclave
          - *backup
EOF

echo -e "${GREEN}✓ Updated $SOPS_CONFIG${NC}"
echo

# Now add the backup key to secrets.yaml
echo -e "${BLUE}Adding backup key to secrets.yaml...${NC}"
echo -e "${YELLOW}You need to manually add the backup key to secrets.yaml${NC}"
echo
echo -e "Run: ${GREEN}sops $SECRETS_DIR/secrets.yaml${NC}"
echo
echo "Then add this entry:"
echo -e "${BLUE}backup_age_key: |${NC}"
cat "$BACKUP_KEY" | sed 's/^/  /'
echo

# Re-key existing secrets
if [ -f "$SECRETS_DIR/secrets.yaml" ]; then
    read -p "Press Enter after you've added the key to secrets.yaml to rekey..."
    
    echo -e "${BLUE}Re-keying existing secrets with both keys...${NC}"
    cd "$SECRETS_DIR"
    
    # This will use the Secure Enclave key to decrypt and re-encrypt with both keys
    sops updatekeys secrets.yaml
    
    echo -e "${GREEN}✓ Secrets re-keyed successfully${NC}"
    echo -e "${GREEN}  Both keys can now decrypt the secrets${NC}"
else
    echo -e "${YELLOW}⚠ No secrets.yaml found - will be encrypted with both keys when created${NC}"
fi

echo
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup Complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo
echo -e "${BLUE}IMPORTANT: Store backup key securely!${NC}"
echo "1. Copy $BACKUP_KEY to a secure location:"
echo "   - Password manager as a secure note"
echo "   - Encrypted USB drive"
echo "   - Secure cloud storage"
echo
echo -e "${BLUE}Next steps:${NC}"
echo "1. Use: ${GREEN}bash $SCRIPT_DIR/secrets-edit-backup.sh${NC} to edit secrets"
echo "2. Backups will be saved to: ${GREEN}$SECRETS_DIR/backups/${NC}"
echo
echo -e "${YELLOW}Security notes:${NC}"
echo "• Keep $BACKUP_KEY in a secure location"
echo "• Anyone with this key can decrypt your secrets"
echo "• The key is also stored encrypted inside secrets.yaml"
echo "• Backup archives can be decrypted with the backup key"
echo
