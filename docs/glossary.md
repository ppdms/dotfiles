# Words used in this repository

- **Settings (the "profile")** — the one private JSON file describing
  a machine: hostname, username, disks, enabled programs and
  services. The file, tool and flake input use the name *profile*. See
  [Settings and secrets](settings-and-secrets.md).
- **Render** — decryption of the settings, followed by production of
  a validated, platform-selected generation of build inputs and an
  editable plaintext file.
- **Seal** — checking of the plain settings file, followed by
  encryption.
- **Flake** — a Nix project with declared inputs (dependency sources)
  and outputs (build products). This repository is one flake;
  `flake.nix` at the root is the single entry point for both machines.
- **Input / `--override-input profile`** — a named dependency of a
  flake. The settings form the `profile` input. Overriding that input
  for one command selects a generated private profile or an example
  profile without changing the public lock file.
- **Placeholder settings** — `settings/placeholder-profile/`: the
  default `profile` input. It builds nothing and, when forced,
  explains the selection mechanism.
- **Home Manager** — the tool that manages a user account (programs,
  program settings, shell) declaratively, as NixOS manages a whole
  machine. `machines/shared/home.nix` defines the common account layer
  for both machines; each machine adds its own part.
- **nix-darwin** — the macOS counterpart of NixOS for system settings.
- **Service account ("principal")** — the dedicated Unix account a
  server service runs as, defined in `services/service-accounts.nix`.
- **Quadlet** — Podman's declarative container format. Its generator
  produces systemd units from files such as
  `services/containers/*.container`.
- **Credential broker** — the server program holding the secrets and
  issuing each service only its own records. The editing command is
  `creds`.
- **Image feed** — a small local git repository of approved,
  digest-pinned container image references. Private state records its
  location and the selected commit.
- **Approved copy** — the frozen code, settings and lock saved by a
  password-confirmed manual switch on the server; the sole input the
  unattended updater may consume.
- **SOPS** — a file-encryption tool used with age identities for the
  Mac's settings and runtime secret files. Linux settings use
  `systemd-creds`, not SOPS.
- **STATE** — shorthand in commands for the private
  state folder; default `~/.local/state/nix-config`.
