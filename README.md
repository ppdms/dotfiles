# Nix config

## Layout

- `systems/darwin`: macOS
- `systems/nixos`: NixOS
- `systems/common`: shared Home Manager config
- `apps`, `bin`, `modules`: programs and reusable config
- `scripts`: local profile, SOPS, backup, and recovery tools

All code is tracked. Machine-specific material is not.

Local state:

```text
~/.local/state/nix-config/
```

It contains the local profile, SOPS policy, encrypted data, and generated files.
Keys live under `~/.config/sops/age/`. I provision and back up both locations
separately; neither is committed.

## Profile

```text
profile-render  # refresh generated files
profile         # render, edit profile.json, and seal it
profile-seal    # seal existing local edits
```

## Secrets

```text
secrets
```

## Rebuild

```text
rebuild
rebuild-update
```

Both aliases render the local profile before rebuilding.

Manual evaluation:

```bash
nix flake show ~/.config/nix/systems/darwin \
  --override-input profile "path:$HOME/.local/state/nix-config"

nix flake show ~/.config/nix/systems/nixos \
  --override-input profile "path:$HOME/.local/state/nix-config"
```

## New machine

```bash
git clone https://github.com/ppdms/dotfiles.git ~/.config/nix
# restore ~/.local/state/nix-config and ~/.config/sops through a private channel
./scripts/render-profile.sh --force
```
