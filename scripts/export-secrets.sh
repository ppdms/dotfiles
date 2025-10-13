#!/usr/bin/env bash
#
# Export Secrets with Backup Key
# Creates a portable backup bundle encrypted with the backup key
#
# Usage: ./export-secrets.sh [output-file]

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
NC='\033[0m'

OUTPUT_FILE="${1:-nix-secrets-backup-$(date +%Y%m%d-%H%M%S).tar.gz.age}"

echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  Export Secrets for Backup/Transfer${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo

# Check dependencies
if ! command -v age &> /dev/null; then
    echo -e "${RED}Error: age is not installed${NC}"
    exit 1
fi

if ! command -v sops &> /dev/null; then
    echo -e "${RED}Error: sops is not installed${NC}"
    exit 1
fi

BACKUP_KEY_ENCRYPTED="$AGE_DIR/backup-keys.txt.age"
BACKUP_KEY_TEMP="$AGE_DIR/.backup-keys.tmp"

if [ ! -f "$BACKUP_KEY_ENCRYPTED" ]; then
    echo -e "${RED}Error: Backup key not found!${NC}"
    echo "Run: ./setup-backup-key.sh first"
    exit 1
fi

# Verify secrets can be decrypted
if [ ! -f "$SECRETS_DIR/secrets.yaml" ]; then
    echo -e "${RED}Error: secrets.yaml not found${NC}"
    exit 1
fi

echo -e "${BLUE}Creating portable backup bundle...${NC}"
echo

# Create temporary directory for export
TEMP_DIR=$(mktemp -d)
trap "rm -rf '$TEMP_DIR'" EXIT

EXPORT_DIR="$TEMP_DIR/nix-secrets-export"
mkdir -p "$EXPORT_DIR"

# Decrypt backup key temporarily (will prompt for passphrase)
echo -e "${YELLOW}Enter backup key passphrase:${NC}"
age -d "$BACKUP_KEY_ENCRYPTED" > "$BACKUP_KEY_TEMP"
trap "shred -u '$BACKUP_KEY_TEMP' 2>/dev/null || rm -P '$BACKUP_KEY_TEMP' 2>/dev/null || rm -f '$BACKUP_KEY_TEMP'; rm -rf '$TEMP_DIR'" EXIT

# Copy encrypted secrets
echo -e "${BLUE}1. Copying encrypted secrets...${NC}"
cp "$SECRETS_DIR/secrets.yaml" "$EXPORT_DIR/"
cp "$SECRETS_DIR/.sops.yaml" "$EXPORT_DIR/"
echo -e "${GREEN}✓ Secrets copied${NC}"

# Copy backup key (decrypted for import)
echo -e "${BLUE}2. Including backup key...${NC}"
cp "$BACKUP_KEY_TEMP" "$EXPORT_DIR/backup-keys.txt"
echo -e "${GREEN}✓ Backup key included${NC}"

# Create README for import
echo -e "${BLUE}3. Creating import instructions...${NC}"
cat > "$EXPORT_DIR/README.txt" << 'EOF'
SOPS Secrets Backup Bundle
==========================

This bundle contains:
1. secrets.yaml - Your encrypted secrets (SOPS format)
2. .sops.yaml - SOPS configuration with key references
3. backup-keys.txt - Age private key for decryption

IMPORT INSTRUCTIONS
-------------------

On the new system:

1. Install dependencies:
   nix-shell -p age sops

2. Create directory structure:
   mkdir -p ~/.config/nix/private/{age,secrets}

3. Move the backup key:
   mv backup-keys.txt ~/.config/nix/private/age/

4. Move secrets:
   mv secrets.yaml ~/.config/nix/private/secrets/
   mv .sops.yaml ~/.config/nix/private/secrets/

5. Test decryption:
   SOPS_AGE_KEY_FILE=~/.config/nix/private/age/backup-keys.txt \
     sops -d ~/.config/nix/private/secrets/secrets.yaml

6. (Optional) Generate new Secure Enclave key:
   age-plugin-se -o ~/.config/nix/private/age/keys.txt
   
7. (Optional) Add both keys to .sops.yaml and re-key:
   cd ~/.config/nix/private/secrets
   # Edit .sops.yaml to include both keys
   sops updatekeys secrets.yaml

SECURITY NOTES
--------------
• This bundle contains your UNENCRYPTED private key
• Keep it secure - treat it like a password
• Delete after importing: shred -u backup-keys.txt
• Consider re-keying secrets after import
• Store this bundle encrypted (the .age file)

For more information, see the main README.md in the nix config repository.
EOF

echo -e "${GREEN}✓ Instructions created${NC}"

# Create tarball
echo -e "${BLUE}4. Creating tarball...${NC}"
cd "$TEMP_DIR"
tar -czf secrets-bundle.tar.gz nix-secrets-export/
echo -e "${GREEN}✓ Tarball created${NC}"

# Encrypt the bundle with the backup key
echo -e "${BLUE}5. Encrypting bundle...${NC}"
age -r "$(grep 'public key:' "$BACKUP_KEY_TEMP" | awk '{print $NF}')" \
    -o "$OUTPUT_FILE" secrets-bundle.tar.gz
echo -e "${GREEN}✓ Bundle encrypted${NC}"

# Get file size
FILE_SIZE=$(du -h "$OUTPUT_FILE" | cut -f1)

echo
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Export Complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo
echo -e "${BLUE}Backup file:${NC} ${GREEN}$OUTPUT_FILE${NC}"
echo -e "${BLUE}Size:${NC} $FILE_SIZE"
echo
echo -e "${BLUE}To restore on another system:${NC}"
echo "  1. Transfer $OUTPUT_FILE"
echo "  2. Run: age -d $OUTPUT_FILE | tar -xzf -"
echo "  3. Follow instructions in nix-secrets-export/README.txt"
echo
echo -e "${YELLOW}Security reminder:${NC}"
echo "• This file is encrypted with your backup key"
echo "• You'll need the backup key passphrase to decrypt it"
echo "• Store in secure location (encrypted USB, cloud storage)"
echo "• The decrypted contents contain your private key"
echo
