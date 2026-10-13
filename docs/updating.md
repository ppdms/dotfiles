# Updating

## Ordinary updates

```bash
bash tools/profile/rebuild.sh --update
```

On either machine this updates the root flake's public dependencies,
then builds and activates the system. Dependency sources are declared
in `flake.nix`; exact revisions are stored in `flake.lock`. On the
server it also selects the approved local image feed's current `HEAD`.

The root lock contains the public placeholder-profile path.
Machine-specific profile paths appear in private deployment locks or
command-line overrides, not the public lock.

## Unattended updates (server only)

The server updates itself at night under narrow rules:

- The only starting point is the **approved copy**: the exact code,
  settings and lock saved by the last password-confirmed manual
  switch, stored under `/var/lib/nix-config/deployment`.
- Within that copy, only the two vendor inputs (nixpkgs and
  Home Manager, on their pinned branches) advance.
- New code from this repository, new settings and new container images
  are outside its reach. Those changes take effect only through a
  manual switch.
- A missing approval, or an approval with unexpected ownership or
  permissions, blocks the update entirely.

Approval is installed during manual activation. The unattended updater
uses that frozen source and profile with newer vendor dependencies. It
can reboot within the configured reboot window.

## Container images

Images update through the image feed, a small local git repository
whose location the settings record. Exact digest pins are committed
to the feed first, under the feed's own admission policy. Then:

```bash
profile update-images --image-feed PATH --revision COMMIT
bash tools/profile/rebuild.sh
```

`--revision` takes a full commit id; no "latest" exists at build time.
A service restarts when its container definition or image changes.

## OMP and Tern

**OMP** uses `omp update` on the server. The helper reads the public
GitHub release metadata, prefetches the Linux executable, and compares
its SHA-256 with the release digest when GitHub supplies one. It updates
`tools/omp-source.json`, builds the package and switches the dedicated
OMP Nix profile. New invocations use that version immediately; running
processes are not restarted. The Mac executable is externally managed.

**Tern** uses `homelab-tern update`. Its downloads require a saved
Stencil login. The helper verifies the published size and SHA-256,
updates `programs/tern/source.json`, and test-builds the package. The
web service changes version at the next system switch. See
[Tern](tern.md).

Both helpers attempt to restore a changed pin if the update fails;
independent edits are preserved. Neither helper runs on a timer.
