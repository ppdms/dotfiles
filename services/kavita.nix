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
  secretHelper = pkgs.callPackage ./tools/homelab-client.nix { };
  kavitaState = "${servicePrincipal.stateRoot}/kavita";
  requireKavitaState = pkgs.writeShellApplication {
    name = "homelab-kavita-state";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      state=${lib.escapeShellArg kavitaState}
      install -d -m 0700 "$state"
      # Startup requires the JWT identity and database/OIDC registrations.
      if [[ ! -s "$state/config/appsettings.json" || ! -s "$state/config/kavita.db" ]]; then
        echo "Kavita start blocked: appsettings.json and kavita.db are required under $state/config; refusing a new signing key or database." >&2
        exit 1
      fi
      chmod 0600 "$state/config/appsettings.json"
      # Trust the private root CA so Kavita (.NET, via SSL_CERT_FILE)
      # can fetch the configured auth issuer discovery. Same pattern as
      # jellyfin. Delivered at runtime, never store.
      runtime="''${XDG_RUNTIME_DIR:?}/homelab-kavita"
      install -d -m 0700 "$runtime"
      cat ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt \
        ${lib.escapeShellArg (toString profileRoot + "/${systemConfig.homelab.caCertificateFile}")} \
        > "$runtime/ssl-ca-bundle.pem"
      chmod 0600 "$runtime/ssl-ca-bundle.pem"
    '';
  };
  renderQuadlet =
    source:
    lib.replaceStrings
      [ "@secret-helper@" "@state-helper@" "@kavita-config@" ]
      [
        "${secretHelper}/bin/homelab-secrets"
        "${requireKavitaState}/bin/homelab-kavita-state"
        "${config.homelab.core.renderConfigDirectory ./config/kavita}"
      ]
      (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.kavita.enable = lib.mkEnableOption "rootless Kavita ebook library on the Books share";

  config = lib.mkIf (config.homelab.kavita.enable && servicePrincipal != null && servicePrincipal.name == "kavita") {
    homelab.core.imageSources = [ ./containers/kavita.container ];
    xdg.configFile."containers/systemd/kavita.container".text =
      renderQuadlet ./containers/kavita.container;

    home.activation.createKavitaState = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg kavitaState}
    '';
  };
}
