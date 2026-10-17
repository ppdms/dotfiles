# Nix configuration

This repository describes two computers:

- **The Mac** — a laptop running macOS.
- **The server** — a home server running NixOS, hosting photos, media,
  files, backups and a handful of other services.

The root flake defines both machines using [Nix](https://nixos.org).
macOS uses nix-darwin and Home Manager; the server uses NixOS and Home
Manager. Shell, Git, editor and development configuration are shared.

## Configuration and private data

The repository contains configuration code and example profiles.
Machine-specific hostnames, accounts, disks, service selections and
preferences come from private settings outside the checkout. The
default state directory is `~/.local/state/nix-config/`.

The `profile` tool decrypts and validates settings, retains an editable
plaintext file, and creates a separate generation containing the
selected machine's build inputs. Both machines accept that generation
through `--override-input profile path:<generation>`.

[`examples/`](examples/) contains fictitious Mac and server profiles
that can be evaluated without private state. The Mac example excludes
secret delivery and refuses activation. No encrypted files are included
in the public source.

Selected settings are readable in the Nix store, including hostnames,
usernames and paths. Runtime credentials are managed separately:
SOPS-encrypted files on the Mac and a credential broker on the server.
Mac build inputs include approved SOPS ciphertext, not decrypted
credentials.

Details: [Settings and secrets](docs/settings-and-secrets.md).

## Building

From the repository root, on the machine being changed:

```bash
bash tools/profile/rebuild.sh                     # rebuild and switch this machine
bash tools/profile/rebuild.sh --no-switch         # prepare inputs; no build or activation
bash tools/profile/edit-profile.sh                # edit; seal after successful editor exit
```

The Mac and the server accept the same wrapper commands. The shell also
provides `rebuild`, `rebuild-update` and `profile-edit` shortcuts.
More: [Daily use](docs/daily-use.md).

## Where things live

| Folder | Contents |
|---|---|
| [`machines/`](machines/) | One folder per computer (`macbook/`, `server/`), plus `shared/` for what both machines receive. |
| [`programs/`](programs/) | One folder per program (editor, terminal, shell, …): its settings, and the small module that installs them. |
| [`services/`](services/) | The server's services: one module each, container definitions in `containers/`, settings in `config/`, helper tools in `tools/`. |
| [`commands/`](commands/) | Small personal commands on the `PATH` (`clipssh`, `pdfsum`, …). |
| [`tools/`](tools/) | Tools that operate this repository — above all `tools/profile/`, which encrypts the settings and prepares builds. |
| [`settings/`](settings/) | The form the private settings must fit: `profile-schema.json` lists every allowed field; `load-profile.nix` checks it again inside Nix. |
| [`examples/`](examples/) | Fictitious machine settings, image pins and a public CA certificate. |
| [`docs/`](docs/) | Configuration, operations, storage and recovery documentation. |

## Documents

- [Daily use](docs/daily-use.md) — rebuilding, updating, checking, rolling back.
- [Settings and secrets](docs/settings-and-secrets.md) — the private
  file; first-time setup on a new machine.
- [Program definitions](docs/adding-a-program.md)
- [Service definitions](docs/adding-a-service.md) (server only)
- [Backups and recovery](docs/backups-and-recovery.md)
- [Updating](docs/updating.md) — including the server's unattended updates.
- [The server, in short](docs/server.md) — structure and trust.
- [Self-built images](docs/self-built-images.md) — building and releasing
  images from a local checkout.
- [SilverBullet customizations](docs/silverbullet.md) — where the editor
  client, service wiring and space style each live.
- [Words used in this repository](docs/glossary.md) — technical terms,
  defined once.
- Application details: [Codex sandbox setting](docs/codex-sandbox.md) ·
  [Tern](docs/tern.md) · [Photos importer](docs/photos-importer.md)
