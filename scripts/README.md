# Local configuration tools

```bash
# Decrypt the local profile into generated state
./scripts/render-profile.sh

# Encrypt profile changes locally
./scripts/seal-profile.sh

# Edit local secrets and create a local backup
./scripts/secrets-edit-backup.sh

# Build the local SOPS policy from local identities
./scripts/setup-backup-key.sh

# Restore secrets and rebuild macOS
zsh ./scripts/recreate-darwin-from-backup.zsh --backup-file /path/to/backup.age
```

These scripts only read and write `~/.local/state/nix-config` and
`~/.config/sops`. No key, policy, ciphertext, manifest, or generated profile is
stored in this repository.
