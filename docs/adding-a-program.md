# Program definitions

Program definitions are grouped by name in [`programs/`](../programs/).
Folders contain configuration assets and, where applicable, a Home
Manager module:

- the program's settings files, in the program's own format
  (`programs/kitty/kitty.conf`, `programs/zed/settings.json`), and
- where Nix must act on them, a `module.nix`: a small Home Manager
  module that installs the program and places its settings.

`programs/git/` contains a module, `programs/neovim/` also contains
templates, and `programs/omp/` contains configuration assets, an updater
and tests.

## Imports and settings

`machines/shared/home.nix` imports the common shell, Git, Neovim,
development, documents and OMP modules. The machine-specific home
modules add platform-specific configuration.

Private application choices are defined under `applications` in
`settings/profile-schema.json`. Some fields have defaults; optional
values such as Codex's sandbox mode may be `null`.

The `systemConfig` argument contains the validated settings for the
selected platform. Program modules consume that argument rather than
loading the private source file.

## Secrets

Credential-shaped field names are rejected by the settings validators.
Mac runtime credentials use SOPS; server service credentials use the
broker. See
[Settings and secrets](settings-and-secrets.md).
