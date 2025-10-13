#!/usr/bin/env bash
#
# Re-key Secrets with Both Keys
# Updates secrets to be encrypted with both Secure Enclave and backup keys
#
# Usage: ./rekey-with-both.sh

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

echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  Re-key Secrets with Multiple Keys${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo

# Check for keys
SE_KEY="$AGE_DIR/keys.txt"
BACKUP_KEY="$AGE_DIR/backup-keys.txt"
BACKUP_KEY_ENCRYPTED="$AGE_DIR/backup-keys.txt.age"

HAS_SE=false
HAS_BACKUP=false

if [ -f "$SE_KEY" ]; then
    SE_PUBLIC_KEY=$(grep "public key:" "$SE_KEY" | awk '{print $NF}')
    echo -e "${GREEN}✓ Secure Enclave key found${NC}"
    echo -e "  Public key: ${BLUE}$SE_PUBLIC_KEY${NC}"
    HAS_SE=true
else
    echo -e "${YELLOW}⚠ Secure Enclave key not found at $SE_KEY${NC}"
fi

# Check for backup key (either encrypted or plain)
if [ -f "$BACKUP_KEY" ]; then
    BACKUP_PUBLIC_KEY=$(grep "public key:" "$BACKUP_KEY" | awk '{print $NF}')
    echo -e "${GREEN}✓ Backup key found (plaintext)${NC}"
    echo -e "  Public key: ${BLUE}$BACKUP_PUBLIC_KEY${NC}"
    HAS_BACKUP=true
elif [ -f "$BACKUP_KEY_ENCRYPTED" ]; then
    echo -e "${BLUE}Backup key found (encrypted)${NC}"
    echo -e "${YELLOW}Decrypting backup key...${NC}"
    age -d "$BACKUP_KEY_ENCRYPTED" > "$BACKUP_KEY"
    BACKUP_PUBLIC_KEY=$(grep "public key:" "$BACKUP_KEY" | awk '{print $NF}')
    echo -e "${GREEN}✓ Backup key decrypted${NC}"
    echo -e "  Public key: ${BLUE}$BACKUP_PUBLIC_KEY${NC}"
    HAS_BACKUP=true
    
    # Clean up after re-keying
    trap "shred -u '$BACKUP_KEY' 2>/dev/null || rm -P '$BACKUP_KEY' 2>/dev/null || rm -f '$BACKUP_KEY'" EXIT
else
    echo -e "${YELLOW}⚠ Backup key not found${NC}"
fi

echo

if [ "$HAS_SE" = false ] && [ "$HAS_BACKUP" = false ]; then
    echo -e "${RED}Error: No keys found!${NC}"
    echo "Run ./setup-backup-key.sh first"
    exit 1
fi

# Create .sops.yaml
SOPS_CONFIG="$SECRETS_DIR/.sops.yaml"
echo -e "${BLUE}Updating SOPS configuration...${NC}"

cat > "$SOPS_CONFIG" << EOF
keys:
EOF

if [ "$HAS_SE" = true ]; then
    cat >> "$SOPS_CONFIG" << EOF
  - &secure_enclave $SE_PUBLIC_KEY
EOF
fi

if [ "$HAS_BACKUP" = true ]; then
    cat >> "$SOPS_CONFIG" << EOF
  - &backup $BACKUP_PUBLIC_KEY
EOF
fi

cat >> "$SOPS_CONFIG" << EOF

creation_rules:
  - path_regex: .*\.yaml$
    key_groups:
      - age:
EOF

if [ "$HAS_SE" = true ]; then
    echo "          - *secure_enclave" >> "$SOPS_CONFIG"
fi

if [ "$HAS_BACKUP" = true ]; then
    echo "          - *backup" >> "$SOPS_CONFIG"
fi

echo -e "${GREEN}✓ Updated $SOPS_CONFIG${NC}"

# Re-key secrets
if [ -f "$SECRETS_DIR/secrets.yaml" ]; then
    echo -e "${BLUE}Re-keying secrets...${NC}"
    cd "$SECRETS_DIR"
    
    # Set the key file based on what we have
    if [ "$HAS_BACKUP" = true ]; then
        export SOPS_AGE_KEY_FILE="$BACKUP_KEY"
    elif [ "$HAS_SE" = true ]; then
        export SOPS_AGE_KEY_FILE="$SE_KEY"
    fi
    
    sops updatekeys secrets.yaml
    
    echo -e "${GREEN}✓ Secrets re-keyed successfully${NC}"
    
    # Verify with both keys
    echo -e "${BLUE}Verifying decryption...${NC}"
    
    if [ "$HAS_SE" = true ]; then
        if SOPS_AGE_KEY_FILE="$SE_KEY" sops -d secrets.yaml > /dev/null 2>&1; then
            echo -e "${GREEN}✓ Secure Enclave key can decrypt${NC}"
        else
            echo -e "${RED}✗ Secure Enclave key cannot decrypt${NC}"
        fi
    fi
    
    if [ "$HAS_BACKUP" = true ]; then
        if SOPS_AGE_KEY_FILE="$BACKUP_KEY" sops -d secrets.yaml > /dev/null 2>&1; then
            echo -e "${GREEN}✓ Backup key can decrypt${NC}"
        else
            echo -e "${RED}✗ Backup key cannot decrypt${NC}"
        fi
    fi
else
    echo -e "${YELLOW}⚠ No secrets.yaml found${NC}"
fi

echo
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Re-keying Complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo

if [ "$HAS_SE" = true ] && [ "$HAS_BACKUP" = true ]; then
    echo -e "${GREEN}✓ Both keys configured${NC}"
    echo "  • Secure Enclave key (daily use with TouchID)"
    echo "  • Backup key (portable, encrypted with passphrase)"
elif [ "$HAS_SE" = true ]; then
    echo -e "${YELLOW}⚠ Only Secure Enclave key configured${NC}"
    echo "  Run ./setup-backup-key.sh to add backup key"
elif [ "$HAS_BACKUP" = true ]; then
    echo -e "${YELLOW}⚠ Only backup key configured${NC}"
    echo "  Run: age-plugin-se -o $AGE_DIR/keys.txt"
    echo "  Then run this script again"
fi

echo
