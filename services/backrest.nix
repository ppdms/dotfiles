{
  config,
  lib,
  pkgs,
  systemConfig,
  servicePrincipal ? null,
  ...
}:

let
  state = "${servicePrincipal.stateRoot}/backrest";
  secrets = pkgs.callPackage ./tools/homelab-client.nix { };
  requireState = pkgs.writeShellApplication {
    name = "homelab-backrest-state";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      state=${lib.escapeShellArg state}
      if [[ ! -s "$state/config/config.json" ]]; then
        echo "Backrest start blocked: config/config.json is missing or empty under $state; configuration and repository identity are required. Refusing a replacement configuration." >&2
        exit 1
      fi
      for directory in "$state"/{config,data,cache,tmp} ${lib.escapeShellArg systemConfig.homelab.restoreDestination}; do
        install -d -m 0700 "$directory"
      done
    '';
  };
in
{
  options.homelab.backrest.enable = lib.mkEnableOption "rootless Backrest backup service";

  config = lib.mkIf (config.homelab.backrest.enable && servicePrincipal != null && servicePrincipal.name == "backrest") {
    homelab.core.imageSources = [ ./containers/backrest.container ];
    homelab.secrets.mutableComponents = [ "backrest" ];
    xdg.configFile = {
      "containers/systemd/backrest.container".text =
        config.homelab.core.renderQuadlet ./containers/backrest.container;
      "containers/systemd/backrest.container.d/runtime.conf" = {
        text = ''
          [Service]
          ExecStartPre=${requireState}/bin/homelab-backrest-state
          ExecStartPre=${secrets}/bin/homelab-secrets prepare backrest
          ExecStopPost=${secrets}/bin/homelab-secrets persist backrest
        '';
        onChange = "homelabChangedContainers+=(backrest.service)";
      };
    };
  };
}
