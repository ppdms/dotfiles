{
  lib,
  writeShellApplication,
  writeText,
  neovim,
  nano,
  systemd,
  openssl,
  coreutils,
  secrets,
}:

let
  editors = writeText "homelab-creds-editors.json" (
    builtins.toJSON {
      vi = "${neovim}/bin/nvim";
      vim = "${neovim}/bin/nvim";
      nvim = "${neovim}/bin/nvim";
      nano = "${nano}/bin/nano";
    }
  );
in
writeShellApplication {
  name = "creds";
  text = ''
    export HOMELAB_LAN_DOMAIN=${lib.escapeShellArg secrets.lanDomain}
    export HOMELAB_PUBLIC_DOMAIN=${lib.escapeShellArg secrets.publicDomain}
    export PYTHONPATH=${lib.escapeShellArg "${secrets}/share/homelab"}
    export HOMELAB_EDITORS=${editors}
    export HOMELAB_SYSTEMCTL=${systemd}/bin/systemctl
    export HOMELAB_OPENSSL=${openssl}/bin/openssl
    export HOMELAB_ENV=${coreutils}/bin/env
    export HOMELAB_SUDO=/run/wrappers/bin/sudo
    exec ${secrets.python}/bin/python3 ${./creds.py} "$@"
  '';
}
