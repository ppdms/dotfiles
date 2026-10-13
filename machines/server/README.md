# The server

A home server running NixOS, hosting the household services in
[`../../services/`](../../services/) as containers.

| File | Contents |
|---|---|
| `system.nix` | The machine: boot, users, SSH (keys only), locale. |
| `runtime.nix` | Service operation: the container engine, the firewall between service accounts, gateway port forwarding, host metrics, the approved unattended-update mechanism. |
| `accounts.nix` | Creation of the account each service runs as, and the builds of the server's own tools (credential broker, image updater, backup helpers). The account data itself is `services/service-accounts.nix`. |
| `home.nix` | The operator account: the services' user-level modules, Tern, the `rebuild` shell shortcuts. |
| `hardware.nix` | Filesystems, kernel modules, microcode — the only file describing this specific hardware. A replacement server starts here. |
| `from-profile.nix` | The builder: it turns the selected settings into this machine's configuration plus the tools the machine hands back. The root `flake.nix` calls it. |

[The server, in short](../../docs/server.md) states the structure;
[Service definitions](../../docs/adding-a-service.md) describes the
modules and their dependencies. Rebuilding and settings editing follow
[Daily use](../../docs/daily-use.md).

Deliberate differences from the Mac: every service runs as a dedicated
account (Mac programs run as the user); secrets live in a credential
broker instead of SOPS files; and the server alone carries the narrow
unattended-update mechanism described in
[Updating](../../docs/updating.md).
