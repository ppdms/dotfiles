{
  config,
  lib,
  pkgs,
  systemConfig,
  caCertificate,
  servicePrincipal ? null,
  ...
}:

let
  secretHelper = pkgs.callPackage ./tools/homelab-client.nix { };
  requireOpencloudState = pkgs.writeShellApplication {
    name = "homelab-opencloud-state";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.util-linux
    ];
    text = ''
      umask 077
      storage=${lib.escapeShellArg systemConfig.homelab.paths.opencloud}
      if ! mountpoint --quiet "$storage"; then
        echo "OpenCloud start blocked: the declared storage view is not mounted." >&2
        exit 1
      fi
      if [[ ! -s "$storage/data/idm/idm.boltdb" ||
            ! -d "$storage/data/storage/metadata" ||
            ! -d "$storage/data/storage/users/users" ]]; then
        echo "OpenCloud start blocked: required IDM/POSIX storage missing or empty; refusing a new store." >&2
        exit 1
      fi
      # homelab-secrets supplies the OpenCloud YAML configuration.
      # No OCIS_* environment overrides or replacement identity.
      runtime="''${XDG_RUNTIME_DIR:?}/homelab-opencloud"
      install -d -m 0700 "$runtime"
      cat ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt \
        ${lib.escapeShellArg (toString caCertificate)} \
        > "$runtime/tls-bundle.crt"
      chmod 0600 "$runtime/tls-bundle.crt"
    '';
  };
  renderQuadlet =
    source:
    lib.replaceStrings
      [ "@secret-helper@" "@state-helper@" "@opencloud-config@" ]
      [
        "${secretHelper}/bin/homelab-secrets"
        "${requireOpencloudState}/bin/homelab-opencloud-state"
        "${config.homelab.core.renderConfigDirectory ./config/opencloud}"
      ]
      (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.opencloud.enable = lib.mkEnableOption "rootless OpenCloud file library";

  config = lib.mkIf (config.homelab.opencloud.enable && servicePrincipal != null && servicePrincipal.name == "opencloud") {
    homelab.core.imageSources = [ ./containers/opencloud.container ];
    xdg.configFile."containers/systemd/opencloud.container".text =
      renderQuadlet ./containers/opencloud.container;

  };
}
