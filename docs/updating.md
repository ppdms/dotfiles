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

Services built from a local checkout do not use the feed at all: their image
is built on the server and released with `homelab-image-build deploy NAME`,
without a system switch. See [Self-built images](self-built-images.md).

## Tree application releases

Tree is a [self-built service](self-built-images.md) that also owns data, so it
has its own command, `tree-dev`, built on the same pipeline as
`homelab-image-build`: the same clean-commit build, seven-day base-image
quarantine, transfer by image ID and `:stable` promotion. What `tree-dev` adds
is below.

Nix owns Tree's accounts, services, migration unit, recovery admission and
installed `tree-dev` command. Application releases remain application-scoped:
they select an immutable locally built image through `:stable`, rather than
rebuilding the entire server for each application commit.

After changing the deployment tooling, install it and its matching service
definitions through the normal, password-confirmed switch. In interactive
Fish, use the same shortcut as on macOS (see [Rebuilding](daily-use.md#rebuilding)
for the Bash wrapper):

```fish
rebuild
```

Then a release is one command, runnable from any working directory:

```bash
tree-dev deploy
```

The configured application checkout must be clean and committed. If the
disposable development stack is running, use `tree-dev down` first; development
data is never promoted. `tree-dev stage` observes changed upstream base images
before their seven-day eligibility window. Local application commits are not
age-gated.

Deployment authorizes, builds and transfers the candidate while the previous
runtime remains available. It then serializes lifecycle changes, blocks new
starts, stops all known writers, and takes a cold checkpoint before migration.
The Nix-owned migration service retains its exact exit result. Its required
root-owned image permission is revoked before shutdown and recovery, so a
delayed start cannot create another writer after the cold check.

A failed build leaves the old runtime running. A failed activation restores
both its image and its checkpointed database, objects, settings and identity,
then restarts it only if it was previously running. The command still exits
nonzero. If recovery itself fails, Tree stays blocked with the pending record
intact. After interruption or resolving a reported recovery problem:

```bash
tree-dev status
tree-dev recover
```

Recovery of an admitted candidate warns that later writes will be discarded;
the failed dataset is retained in a private rescue copy. Checkpoints and
journals live under `/var/lib/homelab/tree-releases`, outside workload-owned
state. Mutable files use independent reflinks/copies; immutable objects use
hardlinks on the same filesystem. Existing manual `.predeploy-*` backups are
not modified. Managed checkpoints and their `checkpoint-*` image tags are
retained, not automatically pruned.

These are local recovery points, not external backups. A NixOS generation
rollback changes configuration and tools; it does not restore Tree's
application image or database. `recover` resolves a pending application
activation, not arbitrary historical rollback.

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
