# Scripts Directory

This directory contains helper scripts for managing secrets and backups.

## 📋 Available Scripts

### Backup System

- **`setup-backup-key.sh`** - One-time setup for the backup key system
  - Generates a backup age key (or uses existing one)
  - Updates `.sops.yaml` to include both Secure Enclave and backup keys
  - Guides you through adding the backup key to `secrets.yaml`
  - Rekeys existing secrets to be encrypted with both keys

- **`secrets-edit-backup.sh`** - Edit secrets with automatic backup
  - Opens `secrets.yaml` in your editor (with SOPS)
  - Rekeys with both Secure Enclave and backup keys
  - Creates timestamped backup of decrypted secrets, encrypted with backup key only
  - **Recommended:** Use via the `secrets` alias in your shell

## 📚 Documentation

- **`README-secrets-backup.md`** - Detailed documentation of the backup system
- **`BACKUP-QUICKSTART.md`** - Quick start guide for setting up backups
- **`README-ssh-agent-security.md`** - SSH agent security details (if you have it)
- **`SETUP-SSH-ASKPASS.md`** - SSH askpass setup instructions (if you have it)

## 🚀 Quick Start

### Setting up Backups

1. **Generate backup key:**
   ```bash
   age-keygen -o ~/.config/nix/private/age/backup-key.txt
   ```

2. **Run setup script:**
   ```bash
   bash ~/.config/nix/public/scripts/setup-backup-key.sh
   ```

3. **Store backup key safely:**
   - Copy `backup-key.txt` to password manager
   - Or save to encrypted USB
   - Or store in secure cloud storage

4. **Use the `secrets` alias to edit:**
   ```bash
   secrets
   ```
   This automatically creates backups in `~/.config/nix/private/secrets/backups/`

### Emergency Recovery

If you lose access to your Secure Enclave key:

```bash
# Decrypt a backup using your backup key
age --decrypt -i /path/to/backup-key.txt \
  ~/.config/nix/private/secrets/backups/secrets.yaml.TIMESTAMP.age > secrets.yaml
```

## 🔐 Security Notes

- **Backup key is stored in two places:**
  1. Encrypted in `secrets.yaml` (accessible with Secure Enclave key)
  2. Cleartext copy stored offline (for emergency recovery)

- **Backup archives contain decrypted secrets**, encrypted with backup key only
  - These can be safely stored anywhere (cloud, USB, etc.)
  - Only your backup key can decrypt them

- **Double encryption:**
  - `secrets.yaml` is encrypted by SOPS with both keys
  - Backup archives are encrypted by age with backup key only
  - You can decrypt secrets with either key

## 🎯 Workflow

```
Daily editing:
  secrets → edit in SOPS → save → automatic rekey + backup

Emergency recovery:
  Get backup key → decrypt archive → restore secrets
```

## 📖 More Information

See the main [README.md](../README.md) for complete documentation of the entire configuration system.
