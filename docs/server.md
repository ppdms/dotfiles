# The server, in short

The NixOS server runs containerized applications and supporting systemd
units. Service modules are in `services/`; system configuration is in
`machines/server/`.

## Structure and access boundaries

- **Workload accounts.** Each workload account has a dedicated home,
  rootless container store and subordinate ID range. Login is
  disabled. Containers within one account share its access scope.
- **HTTP gateway.** nginx routes application web traffic. Authelia
  provides SSO/OIDC and forward authentication for selected routes.
  Tunnel access also requires Cloudflare Access. The individual server
  blocks determine which authentication checks apply; they are not
  identical for every application. Firewall rules restrict direct
  access to declared backend ports.
- **Secrets have one home.** A small broker program holds the
  server's credentials, encrypted to the machine. Each service reads
  only the records declared for it; root and the operator account
  administer the store. No secret passes through Nix in plain text.
- **Container images.** Registry images are digest-pinned and
  distributed into each service's own store before workloads start.
  See [Updating](updating.md).
- **Backups.** Backrest reads read-only views of application data and
  encrypted credential exports. Its account has no decryption identity
  for those exports. Application data can itself contain secrets.
- **Unattended updates are narrow.** At night the machine may advance
  two vendor dependencies inside the last password-approved copy —
  never new code, settings or images. See [Updating](updating.md).
- **Prepared or stopped.** State, credentials and images are prepared
  by dedicated units before services start. Failed preparation leaves
  services stopped rather than half-configured.

## Locations

| Subject | Place |
|---|---|
| The services, their containers and settings | `services/` |
| The accounts services run as | `services/service-accounts.nix`, created by `machines/server/accounts.nix` |
| Boot, disks, firewall, the approved-update mechanism | `machines/server/system.nix`, `machines/server/runtime.nix` |
| The credential broker and other server tools | `services/tools/` |
| Which services are enabled | The private settings; not this repository |

## Credential editing

On the server, `creds <component>` edits one service's records in a
private temporary workspace. `--check` validates without restarting
anything; `--restart` restarts the consuming services around the edit.
Broker access is scoped by caller identity. The editing workspace is
private to the administrator performing the edit.
