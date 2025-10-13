# How to set up passphrase-protected backup for secrets.yaml

## 1. Generate a passphrase-protected age key

```bash
age-keygen -p > ~/.config/nix/private/age/backup-key.txt
```
You will be prompted for a passphrase. Save this passphrase somewhere safe.

## 2. Store the passphrase in secrets.yaml (encrypted with Secure Enclave)

Add a new entry to your secrets.yaml (using your normal sops editing):

```yaml
backup_age_passphrase: your-long-passphrase-here
```

## 3. Add the backup key to your .sops.yaml

Edit `~/.config/nix/.sops.yaml` to include both your Secure Enclave public key and the backup key public key in the `key_groups` for secrets.yaml.

## 4. Use the wrapper script

Use the script `scripts/secrets-edit-backup.sh` instead of editing secrets.yaml directly. This will:
- Decrypt the backup passphrase from secrets.yaml
- Edit secrets.yaml with sops
- Rekey secrets.yaml to include both keys
- Create a backup encrypted with the backup key only

## 5. (Optional) Alias

Add this to your shell config:
```fish
alias secrets 'bash ~/.config/nix/public/scripts/secrets-edit-backup.sh'
```

Now, every time you run `secrets`, your secrets are backed up and rekeyed for both keys.
