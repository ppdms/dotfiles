# SilverBullet: where each customization lives

SilverBullet is customized in five places. Change a thing where it lives; none
of them is generated from another.

| What | Where | Changes |
|---|---|---|
| **Editor client** (Source / Live Edit / Reading, Crepe, native widgets) | `~/Developer/silverbullet` — a fork of upstream 2.11.1 | Editing behaviour, the editor's own styling, Markdown parsing, Reading mode |
| **The image** | `Dockerfile.source` in that fork | How the fork becomes a container image |
| **Service wiring** (container, mounts, ingress) | `services/silverbullet.nix`, `services/containers/silverbullet.container`, `services/self-built.nix`, `services/config/ingress/nginx/sites*/notes*.conf` (this repository) | Image source, ports, volumes, health |
| **Which image runs** | the private settings, `homelab.selfBuilt.silverbullet` | Checkout path; `serve` on or off |
| **Space configuration and style** | the space's own `CONFIG.md`, `STYLE.md` | Mode commands/shortcuts, fonts and colours, `--live-editor-*` / `--reader-*` variables |

The space's own tooling lives alongside them in the vault.

The server runs as the `homelab-silverbullet` account, not as your user.
`~/notes` is that account's vault: edit it through SilverBullet, or with `sudo`.

## Everyday use, once adopted

```bash
cd ~/Developer/silverbullet
# ... edit, test ...
git commit -am "..."                      # only committed code is built
homelab-image-build deploy silverbullet   # build, activate, health-check, roll back on failure
```

Reload the browser afterwards. `homelab-image-build rollback silverbullet`
returns to the previous image. The mechanism is described in
[Self-built images](self-built-images.md).

## Adopting the self-built image

The image's base images (`node`, `rust`, `alpine`) must have been observed for
seven days before the first build may use them. Start that clock first; the
service keeps running as it does today meanwhile.

1. **Start the clock now.** The installed updater already understands the new
   Dockerfile:

   ```bash
   homelab-image-update tree-inputs --dockerfile ~/Developer/silverbullet/Dockerfile.source
   ```

   It asks for your sudo password and prints each base as quarantined until a
   date seven days away. After the next rebuild the same command is
   `homelab-image-build stage silverbullet`, and a daily timer repeats it. Do not
   change the `FROM` tags in `Dockerfile.source` casually: a new tag restarts the
   clock.

2. **Commit the fork.** Builds use `git archive` of `HEAD`; uncommitted work
   never enters an image.

3. **Declare it, not yet serving.** `bash tools/profile/edit-profile.sh`, add
   under `homelab`:

   ```json
   "selfBuilt": {
     "silverbullet": { "checkout": "/home/you/Developer/silverbullet", "serve": false }
   }
   ```

   then `rebuild`. This installs `homelab-image-build`, creates the builder
   account and starts observing the bases. SilverBullet itself is unchanged.

4. **After the clock has run** (`homelab-image-build deploy silverbullet`
   refuses before then, touching nothing):

   ```bash
   homelab-image-build deploy silverbullet
   homelab-image-build status silverbullet     # :stable revision, health
   ```

   With `serve` still `false` this only builds and stages the image.

5. **Switch over.** Set `"serve": true`, `rebuild`. The unit restarts into
   `localhost/silverbullet:stable`, which already exists.

6. **Delete the bridge** (below) — it is only needed until step 5.

To go back to the upstream image at any point, set `serve` to `false` or remove
the entry and rebuild.

## The bridge (until step 5)

Today the editor runs through a bridge: a client binary built outside the image
and mounted over the upstream image's own, declared as a drop-in in
`services/silverbullet.nix` and installed by
`services/tools/silverbullet-client-deploy.sh`. It exists only because the first
self-built image cannot be built for a week. To refresh it meanwhile, build the
binary from the same `Dockerfile.source` and install it:

```bash
cd ~/Developer/silverbullet
podman build -f Dockerfile.source --target server -t localhost/sb-bridge .
c=$(podman create localhost/sb-bridge)
podman cp "$c":/src/target/release/silverbullet /tmp/silverbullet && podman rm "$c"
sudo bash ~/dotfiles/services/tools/silverbullet-client-deploy.sh /tmp/silverbullet
```

When `serve` is `true` the drop-in is no longer rendered. Then delete, in this
repository, the `10-editor-modes.conf` block in `services/silverbullet.nix`, the
script `services/tools/silverbullet-client-deploy.sh`, and (as root) the
`editor-modes/` directory under the account's `state/silverbullet/`.
