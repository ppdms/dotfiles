# Backups and recovery

Backrest runs restic. On the server, its account reads per-service
read-only data views and age-encrypted credential exports. It has no
decryption identity for those exports. On the Mac, Backrest runs as a
system service against the configured backup destination.

## Backup contents and limits

- **Application data can contain secrets.** A service's data folder
  may hold logged-in sessions or saved tokens. Read access to the
  backups is read access to those.
- **A secrets backup alone is not a machine backup.** Mac restoration
  additionally requires the separately kept encrypted settings, the
  SOPS policy and the matching age identity. Losing the only matching
  decryption identity makes its ciphertext unrecoverable.
- **Creation and restoration are different operations.** A successful
  backup does not, by itself, demonstrate a successful restoration.

## Mac restoration

```bash
zsh tools/profile/recreate-darwin-from-backup.zsh \
  --backup-file FILE --nix-dir "$PWD" [--skip-update] [--no-switch]
```

The command decrypts the backup bundle, re-encrypts its secret files
with the existing SOPS policy, renders the separately retained profile,
and invokes the rebuild wrapper. With `--skip-update --no-switch`, it
still restores private files but does not update dependencies or
activate the system.

Related helpers are in `tools/profile/`. `secrets-edit-backup.sh`
encrypts a bundle of the updated secret files after successful edits;
`setup-backup-key.sh` regenerates the SOPS policy from existing
identities.

## Server restoration

System configuration is built from this repository and the selected
profile. Application data and runtime credentials have separate
recovery paths. Each Linux seal writes a portable age-encrypted profile
snapshot to `STATE/backups/`.

`homelab-backrest-repository` discovers an existing restic repository
and indexes its snapshots without initialization or pruning.
`homelab-backrest-provision` imports private file inputs and can check
them with `--check` or import without starting services with
`--no-start`. Backrest provides the application-data restore interface.

Preparation units run before services start. Missing state,
credentials or images leave services stopped rather than starting
them empty.

Home Assistant plaintext removal requires verified encrypted copies of
both the active credentials and any differing physical `secrets.yaml`.
`homelab-credential-broker import-seed homeassistant` stages the physical
copy as `.homeassistant-physical.cred` in the private seed directory.

## TLS credential names

The nginx credential contract uses `tls-public.crt` and
`tls-public.key`.
Provisioning recipes use `publicCertificateFile` and `publicKeyFile`.
