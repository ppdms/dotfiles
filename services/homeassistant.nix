{
  config,
  lib,
  pkgs,
  systemConfig,
  servicePrincipal ? null,
  ...
}:

let
  secretHelper = pkgs.callPackage ./tools/homelab-secrets.nix {
    lanDomain = systemConfig.homelab.lanDomain;
    publicDomain = systemConfig.homelab.publicDomain;
  };
  haState = "${servicePrincipal.stateRoot}/homeassistant";
  haRestore = "${systemConfig.homelab.restoreRoot}/homeAssistant";
  requireHaState = pkgs.writeShellApplication {
    name = "homelab-homeassistant-state";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      state=${lib.escapeShellArg haState}
      # Root preparation owns the protected ancestry and configuration.
      # The service may validate the configuration, never recreate it.
      if [[ ! -s "$state/config/configuration.yaml" ]]; then
        echo "Home Assistant start blocked: configuration.yaml missing under $state/config; refusing a new store." >&2
        exit 1
      fi
      if [[ ! -f "$state/config/secrets.yaml" || -L "$state/config/secrets.yaml" ]]; then
        echo "Home Assistant start blocked: protected secrets mount target is missing." >&2
        exit 1
      fi
      # The container overlays the root-owned mount target with its own
      # runtime broker record; it cannot read or chmod the placeholder.
    '';
  };
  requireEsphomeState = pkgs.writeShellApplication {
    name = "homelab-esphome-state";
    runtimeInputs = [ pkgs.coreutils pkgs.findutils ];
    text = ''
      umask 077
      state=${lib.escapeShellArg "${haState}/esphome"}
      restore=${lib.escapeShellArg "${haRestore}/esphome"}
      if [[ ! -d "$state" || -z "$(find "$state" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        if [[ ! -d "$restore" || -z "$(find "$restore" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
          echo "ESPHome start blocked: device tree missing or empty under both $state and $restore; refusing a new store." >&2
          exit 1
        fi
        install -d -m 0700 "$state"
        cp -a --no-preserve=ownership "$restore/." "$state/"
        chmod 0700 "$state"
      fi
      secrets="''${XDG_RUNTIME_DIR:?}/homelab-secrets/esphome"
      {
        printf 'ESPHOME_USERNAME=%s\n' ${lib.escapeShellArg systemConfig.homelab.esphomeUsername}
        printf 'ESPHOME_PASSWORD=%s\n' "$(cat "$secrets/dashboard-password")"
      } > "$secrets/dashboard.env"
      chmod 0600 "$secrets/dashboard.env"
    '';
  };
  renderQuadlet = source: lib.replaceStrings
    [ "@secret-helper@" "@state-helper@" ]
    [
      "${secretHelper}/bin/homelab-secrets"
      (if servicePrincipal.name == "homeassistant" then
        "${requireHaState}/bin/homelab-homeassistant-state"
      else
        "${requireEsphomeState}/bin/homelab-esphome-state")
    ]
    (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.homeassistant.enable = lib.mkEnableOption "the principal's Home Assistant or ESPHome service";

  config = lib.mkIf (config.homelab.homeassistant.enable && servicePrincipal != null) (lib.mkMerge [
    (lib.mkIf (servicePrincipal.name == "homeassistant") {
      homelab.core.imageSources = [ ./containers/homeassistant.container ];
      xdg.configFile."containers/systemd/homeassistant.container".text =
        renderQuadlet ./containers/homeassistant.container;
    })
    (lib.mkIf (servicePrincipal.name == "esphome") {
      homelab.core.imageSources = [ ./containers/esphome.container ];
      xdg.configFile."containers/systemd/esphome.container".text =
        renderQuadlet ./containers/esphome.container;
    })
  ]);
}
