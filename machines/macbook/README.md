# The Mac

A laptop running macOS, managed with nix-darwin and Home Manager.

| File | Contents |
|---|---|
| `system.nix` | The machine: users, Finder defaults, Touch ID for sudo, Homebrew, the backup service, encrypted rclone and Backrest setup. |
| `home.nix` | The user account: Mac-only programs and settings (Photos importer, Hammerspoon, kitty and Ghostty themes, Codex and Claude wiring, network modes). |
| `from-profile.nix` | The builder: it turns the selected settings into this machine's configuration. The root `flake.nix` calls it. |

The layer common to both machines lives in [`../shared/`](../shared/);
per-program settings live in [`../../programs/`](../../programs/).
Rebuilding and settings editing follow
[Daily use](../../docs/daily-use.md).

Mac-only subjects: [Photos importer](../../docs/photos-importer.md) ·
[Codex sandbox setting](../../docs/codex-sandbox.md) ·
[secrets and restoration](../../docs/backups-and-recovery.md).

Two deliberate differences from the server: Mac secrets are SOPS files
decrypted with an age key (the server uses a credential broker), and
the Mac has no unattended updating — every Mac rebuild is an explicit
command.
