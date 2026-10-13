# Service definitions (server only)

Containerized workloads combine profile switches, Home Manager
modules, Quadlet files and dedicated accounts.
`services/readeck.nix` and `services/containers/readeck.container`
illustrate the module and container definition.

## The four pieces

1. **The switch.** Service switches appear under `homelab.services`
   in the selected NixOS profile. The schema defaults them to `false`.
   Enabling a switch selects the corresponding account definitions.
2. **The module.** `services/<name>.nix` is a Home Manager module
   active only when the service is switched on *and* the current
   account is that service's account. It places the container
   definition and creates the state folders (mode 0700). Shared
   plumbing — image checks, container rendering, state protection —
   lives in `services/core.nix`.
3. **The container.** `services/containers/<name>.container` is a
   Quadlet file. Registry image references are rendered from the
   selected digest pins. Local Tree development images have a separate
   build workflow.
4. **The account.** Services run under dedicated accounts, each with
   its own home and container store. The account data lives in
   `services/service-accounts.nix`; `machines/server/accounts.nix`
   creates the accounts. Sidecar containers share the parent service's
   account. The account registry assigns subordinate ID ranges by slot.

A service's own settings files live in `services/config/<name>/` and
are copied in by the module, as in `services/config/kavita/`.

## Images

The image feed is a separate local Git repository containing approved
image pins. Its location and selected commit are recorded in private
state. A dedicated account fetches exact OCI archives and distributes
them into each service's container store before workloads start.
`profile update-images` selects a feed commit; it does not pull a
floating tag during system evaluation. Registry credentials stay with
the updater. See [Updating](updating.md).

## Secrets

The account registry declares the credential records (*components*)
available to each service. `creds <component>` edits records on the
server. Services can read their declared scopes and update only fields
explicitly permitted for them. See
[Settings and secrets](settings-and-secrets.md).

## Reachability

nginx is the HTTP gateway. Its module is `services/ingress.nix`, and
the server blocks live under `services/config/ingress/`. Authentication
and transport depend on the individual server block: LAN access,
tunnel access, Authelia forward authentication and application login
are not interchangeable.

## Verification

```bash
bash tools/profile/rebuild.sh --no-switch   # prepare inputs; no build or activation
bash tools/profile/rebuild.sh               # build and switch
sudo systemctl --user -M homelab-readeck@ status readeck.service
```

The service manager starts only after state preparation, credentials
and image distribution succeed. A failed preparation leaves the
service stopped.
