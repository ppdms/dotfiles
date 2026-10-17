#!/usr/bin/env bash
# Vacuum Nix store, package managers, and developer tool caches.
set -euo pipefail

echo "==> Cleaning user Nix profiles..."
nix-collect-garbage -d

echo "==> Cleaning system Nix store..."
sudo nix-collect-garbage -d

echo "==> Optimising Nix store..."
sudo nix store optimise

if command -v uv >/dev/null 2>&1; then
    echo "==> Cleaning uv cache..."
    uv cache clean
fi

if command -v go >/dev/null 2>&1; then
    echo "==> Cleaning Go build cache..."
    go clean -cache
fi

if command -v brew >/dev/null 2>&1; then
    echo "==> Cleaning Homebrew cache..."
    brew cleanup -s --prune=all
fi

echo "==> Vacuum complete."
