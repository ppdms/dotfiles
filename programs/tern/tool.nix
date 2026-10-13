{
  lib,
  writeShellApplication,
  curl,
  nix,
  openssl,
  secrets,
  # The dotfiles checkout `homelab-tern update` pins into.
  checkout,
}:

writeShellApplication {
  name = "homelab-tern";
  runtimeInputs = [ nix ];
  text = ''
    export HOMELAB_LAN_DOMAIN=${lib.escapeShellArg secrets.lanDomain}
    export HOMELAB_PUBLIC_DOMAIN=${lib.escapeShellArg secrets.publicDomain}
    export PYTHONPATH=${lib.escapeShellArg "${secrets}/share/homelab"}
    export HOMELAB_OPENSSL=${openssl}/bin/openssl
    export HOMELAB_CURL=${curl}/bin/curl
    export HOMELAB_NIX=${nix}/bin/nix
    export HOMELAB_NIX_STORE=${nix}/bin/nix-store
    export HOMELAB_CHECKOUT=${lib.escapeShellArg checkout}
    exec ${secrets.python}/bin/python3 -B ${./tool.py} "$@"
  '';
}
