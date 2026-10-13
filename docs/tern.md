# Tern

Tern is an optional, closed-beta program on the server, restricted to
the operator account and off by default. The settings switch it per
machine (`homelab.services.tern`); the public example never enables
it. The code lives in `programs/tern/`: module, pinned package, and
the `homelab-tern` helper.

## The helper's purpose

Tern downloads sit behind a sign-in, so Nix cannot fetch them as
ordinary packages. The helper holds a saved login and performs the
fetching:

```bash
homelab-tern login            # once: prints a link and a code for sign-in
homelab-tern update           # fetch, verify against the publisher, pin, test-build
homelab-tern update --check   # compare the pin with the latest release; change nothing
```

The saved login token is single-use and rotates at every refresh. The
helper stores the successor before using anything the refresh
returned, takes a lock, and never runs on a timer. A rejected session
requires a fresh `login` before `update` succeeds again.

The updated package pin is selected at the next rebuild. Changing the
pin alone does not replace running processes. A session daemon can
remain on its previous executable after the web service changes
version.

## The web token

`tern-web`, the browser front end, shares a token with the web
gateway. The token is the nginx credential field `omp-web-token.conf`.
The separate `tern` record holds the saved Stencil refresh token.

```bash
homelab-tern web-token generate     # --rotate replaces an existing token
systemctl --user restart tern-web
sudo systemctl --user -M homelab-nginx@ restart nginx.service
```

Without the web token, `tern-web` refuses to start. If the gateway's
token field is unset, nginx answers 503 for `/wire` requests.
