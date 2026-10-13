{
  config,
  lib,
  pkgs,
  systemConfig,
  profileRoot,
  servicePrincipal ? null,
  ...
}:

let
  secretHelper = pkgs.callPackage ./tools/homelab-secrets.nix {
    lanDomain = systemConfig.homelab.lanDomain;
    publicDomain = systemConfig.homelab.publicDomain;
  };
  alloyState = "${servicePrincipal.stateRoot}/alloy/data";
  requireAlloyState = pkgs.writeShellApplication {
    name = "homelab-alloy-state";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      state=${lib.escapeShellArg alloyState}
      install -d -m 0700 "$state"
      # Data lives in an account-owned Podman volume, not this host directory.
      # secrets helper materializes one file per field; Alloy needs env.
      secrets="''${XDG_RUNTIME_DIR:?}/homelab-secrets/alloy/env"
      envfile="''${XDG_RUNTIME_DIR:?}/homelab-secrets/alloy/alloy.env"
      : > "$envfile"
      chmod 0600 "$envfile"
      for name in GRAFANA_CLOUD_LOKI_URL GRAFANA_CLOUD_LOKI_USER GRAFANA_CLOUD_API_KEY GRAFANA_CLOUD_PROM_URL GRAFANA_CLOUD_PROM_USER GRAFANA_CLOUD_READ_TOKEN; do
        value=$(cat "$secrets/$name")
        printf '%s=%s\n' "$name" "$value" >> "$envfile"
      done
      chmod 0600 "$envfile"
    '';
  };
  renderQuadlet =
    source:
    lib.replaceStrings
      [ "@secret-helper@" "@state-helper@" "@alloy-config@" ]
      [
        "${secretHelper}/bin/homelab-secrets"
        "${requireAlloyState}/bin/homelab-alloy-state"
        "${config.homelab.core.renderConfigDirectory ./config/alloy}"
      ]
      (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.alloy.enable = lib.mkEnableOption "rootless Alloy journal shipper to Grafana Cloud";

  config = lib.mkIf (config.homelab.alloy.enable && servicePrincipal != null && servicePrincipal.name == "alloy") {
    homelab.core.imageSources = [ ./containers/alloy.container ];
    xdg.configFile."containers/systemd/alloy.container".text =
      renderQuadlet ./containers/alloy.container;
    xdg.configFile."containers/systemd/alloy-data.volume".text =
      config.homelab.core.renderConfig ./containers/alloy-data.volume;

    home.activation.createAlloyState = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg alloyState}
    '';
  };
}
