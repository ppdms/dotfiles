# Daily use

All commands run from the repository root, on the machine being
changed. The Mac and the server accept the same commands.

## Rebuilding

```bash
bash tools/profile/rebuild.sh
```

A rebuild decrypts and validates the settings, builds the machine and
activates the result. On Linux the build runs without privileges before
the password-confirmed switch.

Repository wrappers run the profile helper and schema packaged from their
own Git checkout, not an already-installed `profile` on `PATH`. This keeps
rendered inputs compatible with the configuration being rebuilt.

```bash
bash tools/profile/rebuild.sh --no-switch         # prepare inputs; no build or activation
bash tools/profile/rebuild.sh --update            # fetch newer public dependencies first
bash tools/profile/rebuild.sh --platform nixos    # name the machine type explicitly
```

The shell provides the same operations as `rebuild`, `rebuild-update`,
`profile-edit`, `profile-render` and `profile-seal`. Home Manager
installs their generated configuration during activation. Repository
edits alone do not change the installed Fish configuration or functions
in an open session.

Activation installs the shell configuration. A login shell loads it:

```fish
exec fish -l
```

`--no-switch` prepares inputs without installing shell configuration.

## Rebuild effects

`--no-switch` writes rendered state and, on Linux, the deployment
wrapper. It does not evaluate or build the complete system.

On the server, a password-confirmed rebuild also saves an approved
deployment containing the selected source, profile and lock. The
unattended updater later advances vendor dependencies in that
deployment (see [Updating](updating.md)).

A server rebuild that restarts the units guarding service state pauses
Backrest for the switch and attempts to start it again afterwards,
including after a failed switch. A backup in progress at that moment is
interrupted.

## Editing the settings

```bash
bash tools/profile/edit-profile.sh
```

Decryption, an editor session (`$VISUAL`, then `$EDITOR`, then
`nvim`), then — on successful exit — checking and re-encryption. A
failed edit keeps the edited plain file and leaves the previous
encrypted copy untouched; `bash tools/profile/seal-profile.sh` seals
the corrected file. The settings file itself is described in
[Settings and secrets](settings-and-secrets.md).

## Checking the examples

The example profiles can be selected without private state:

```bash
# Server system build (requires an x86_64-linux builder):
nix build --no-link --no-write-lock-file \
  --override-input profile path:./examples/server \
  .#nixosConfigurations.example-server.config.system.build.toplevel

# Mac system evaluation (does not activate the configuration):
nix eval --raw --no-write-lock-file \
  --override-input profile path:./examples/macbook \
  .#darwinConfigurations.example-laptop.system.drvPath
```

The Mac example is non-deployable: it contains no ciphertext, omits
SOPS secret delivery, and rejects both system and Home Manager
activation. An unmarked production profile still requires its private
encrypted inputs.

## Tests

```bash
# Tracked working-tree files: known credentials, ciphertext and private identifiers:
bash tools/check-secrets.sh

# Settings tool, under its own bundled Python:
nix run .#profile -- python -m unittest discover -s tools/profile

# Server tools:
nix run .#profile -- python -m unittest discover -s services/tools

# Small program tools:
python3 -m unittest discover -s programs/omp
python3 -m unittest discover -s programs/codex   # requires tomlkit
```

The secret check reports matching filenames and fails on findings,
scan errors or untracked files. Public certificates are allowed;
encrypted files are not. It does not inspect every credential format
or certify the absence of secrets.

Server suites exercise Linux-specific socket and filesystem behavior.
The approval-boundary suite requires root and the
`APPROVED_DEPLOYMENT_ARTIFACT` and `APPROVED_DEPLOYMENT_RUNTIME` build
outputs. Some suites explicitly skip unavailable tools; not every
platform requirement has a skip condition.

## Rollback

- Server: `sudo nixos-rebuild switch --rollback`
- Mac: `sudo darwin-rebuild switch --rollback`

A rollback activates a previous system generation. It does not rewrite
the profile tool's private settings. Activation scripts and services
still run, so a rollback is not a restoration of application data.
