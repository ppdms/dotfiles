# Settings and secrets

The configuration separates settings from credentials:

- **Settings** describe a machine: hostname, username, disks, enabled
  programs and services, folder paths, preferences.
- **Secrets** are passwords, keys and tokens.

Nix receives selected, validated settings and, on the Mac, approved
SOPS ciphertext. Decrypted runtime credentials are excluded from those
inputs.

## Private state

Settings have an encrypted source and an editable plaintext copy.
Both are stored outside the checkout:

```text
~/.local/state/nix-config/        (STATE below)
  profile.json                    plain copy, kept for editing
  profile.sops.yaml               encrypted copy (Mac)
  profile.cred                    encrypted copy (server)
  backups/                        extra encrypted snapshots
  .profile-inputs/<id>/           separate generations of selected build inputs
  input -> .profile-inputs/<id>   link to the current generation
  deployment/                     server only: the frozen build recipe
```

The state directory comes from `--state-dir`, then
`NIX_CONFIG_STATE_DIR`, then `${XDG_STATE_HOME:-$HOME/.local/state}/nix-config`.

Every allowed field is listed in
[`settings/profile-schema.json`](../settings/profile-schema.json). The
schema is used by the `profile` tool in Python and by
`settings/load-profile.nix` inside Nix. Platform settings appear under
`profiles.darwin` or `profiles.nixos`; only the selected platform's
block is included in the generated Nix input.

The operations on the file:

| Command | Effect |
|---|---|
| `bash tools/profile/render-profile.sh` | Decrypt, validate, write the editable file and a new generation of build inputs |
| `bash tools/profile/edit-profile.sh` | Decrypt, open an editor, check and re-encrypt on successful exit |
| `bash tools/profile/seal-profile.sh` | Check the plain copy and encrypt it |
| `profile query --field input` | Print the physical directory of the current generation |

Storage properties:

1. **Nix receives the generation, not the state directory.** Wrappers
   select the physical path printed by `profile query --field input`.
   The state directory contains additional private files; the `input`
   link is not the physical directory required by the Nix path input.
2. **The Nix store copy is readable by every account on the machine.**
   Keeping settings outside Git does not hide the selected metadata
   from other local accounts.
3. **Sealing does not delete the plain copy.** `profile.json` remains
   on disk for editing, inside a mode-0700 folder. Generated files are
   mode 0600, writable by their owner; previous generations remain
   present for concurrent builds. A backup including `profile.json`
   contains plaintext metadata.

Field names resembling credentials (`password`, `apiKey`,
`accessToken`, …) are rejected, as are private keys and passwords
embedded in URLs. These checks are not a general secret detector.

## Secrets: a different vault on each machine

Runtime secrets — passwords, private keys, tokens, TLS keys — appear
nowhere in the settings and nowhere in Nix in plain text.

**On the Mac**, secrets live in SOPS-encrypted files beside the
settings (`secrets.sops.yaml`, `rclone.ini`). Only the encrypted files
travel with a build; SOPS decrypts them during activation or at runtime
using an existing age identity. `bash tools/profile/secrets-edit-backup.sh`
edits them and creates an encrypted bundle of the updated files.

These encrypted files belong to private state, not the public source.
The public Mac example contains settings only, excludes SOPS secret
delivery, and refuses system and Home Manager activation.

**On the server**, secrets live in a credential store guarded by a
small broker program (`homelab-credentials`). Each service runs under
a dedicated account and reads only the records declared for it. Root
and the operator account administer the store; the store is encrypted
to the machine (TPM) and to the broker's account. On the server,
`creds <component>` edits one service's records and
`creds <component> --check` validates them without restarting
anything.

Backups read service *data*, and
application data sometimes contains embedded secrets (a logged-in
session, a saved token). Backup access is therefore real access to
that data, even though the backup program cannot decrypt the
credential store itself.

## Bootstrap

`seal --bootstrap` validates and encrypts an explicit JSON file.
`rebuild --no-switch` subsequently renders it and prepares build inputs:

```bash
bash tools/profile/seal-profile.sh --bootstrap /path/to/new-profile.json
bash tools/profile/rebuild.sh --platform nixos --no-switch
```

The example profiles document the required fields, not provisioned
runtime state. Linux sealing requires an existing systemd credential
identity, a TPM, the selected local image-feed commit and a backup age
recipient. Rendering also requires the configured trust certificate.
Mac sealing requires an existing SOPS policy and age identity.
Rendering additionally requires the two encrypted secret files.

The Mac uses the same wrappers with `--platform darwin`. Restoration is
described in [Backups and recovery](backups-and-recovery.md).

## The `profile` command

```text
profile {render|edit|seal|project|query|update-images} [--platform darwin|nixos]
        [--state-dir PATH] [--backend sops|systemd-creds] [--identity PATH]
        [--bootstrap PATH] [--force] [--output PATH] [--field FIELD]
        [--image-feed PATH --revision COMMIT] [--backrest-config PATH]
```

- `render` refuses to overwrite locally changed state unless `--force`.
- `edit` resolves `--state-dir` from the flag, then
  `NIX_CONFIG_STATE_DIR`, then `XDG_STATE_HOME`, once, for the whole
  run. A failed edit keeps the edited file and leaves the previous
  encrypted copy intact.
- `seal` on the server also writes an age-encrypted snapshot to
  `backups/`. `--bootstrap PATH` seals an explicit plain file; there
  is no silent fallback.
- `project --output FILE` writes the exact JSON Nix would receive. It
  is a review artifact, not a buildable input.
- `query --field` prints `hostname`, `checkout`, or `input`.
- `profile python SCRIPT [ARGS...]` runs a script with the bundled
  interpreter — the same one the tool itself uses.
- `bash tools/profile/convert-profile.sh` validates Darwin metadata
  with an explicit checkout and compatibility integer. `--output`
  writes private JSON; `--seal` backs up the ciphertext, seals and
  renders without activation.
- No operation creates an encryption identity. Restoring an encrypted
  source requires its existing identity; a portable age snapshot can
  be decrypted with the corresponding backup identity and sealed to
  replacement machine credentials.
