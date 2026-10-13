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
  termixState = "${servicePrincipal.stateRoot}/termix";
  termixRestore = "${systemConfig.homelab.restoreRoot}/termix/data";
  requireTermixState = pkgs.writeShellApplication {
    name = "homelab-termix-state";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      state=${lib.escapeShellArg termixState}
      restore=${lib.escapeShellArg termixRestore}
      install -d -m 0700 "$state"
      # Termix identity requires the encrypted app database, its generated
      # keys (.env), and stored SSH connection material.
      # Startup must not create a replacement store.
      if [[ ! -s "$state/data/db.sqlite.encrypted" ]]; then
        if [[ ! -s "$restore/db.sqlite.encrypted" ]]; then
          echo "Termix start blocked: db.sqlite.encrypted missing under both $state/data and $restore; refusing a new store." >&2
          exit 1
        fi
        install -d -m 0700 "$state/data"
        cp -a --no-preserve=ownership "$restore/." "$state/data/"
      fi
      if [[ ! -s "$state/data/.env" ]]; then
        echo "Termix start blocked: .env database encryption identity is missing; refusing replacement keys." >&2
        exit 1
      fi
      chmod 0600 "$state/data/.env"
      # Trust the private root CA so Termix (Node, via NODE_EXTRA_CA_CERTS)
      # can exchange codes at the configured auth issuer. Delivered at runtime.
      runtime="''${XDG_RUNTIME_DIR:?}/homelab-termix"
      install -d -m 0700 "$runtime"
      cat ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt \
        ${lib.escapeShellArg (toString profileRoot + "/${systemConfig.homelab.caCertificateFile}")} \
        > "$runtime/ssl-ca-bundle.pem"
      chmod 0600 "$runtime/ssl-ca-bundle.pem"
      # Termix reads the OIDC secret from env, not *_FILE. Assemble a
      # Quadlet EnvironmentFile consumed alongside the secret volume.
      secrets="''${XDG_RUNTIME_DIR:?}/homelab-secrets/termix"
      {
        printf 'OIDC_CLIENT_SECRET=%s\n' "$(cat "$secrets/oidc-client-secret")"
      } > "$secrets/termix.env"
      chmod 0600 "$secrets/termix.env"
    '';
  };
  renderQuadlet =
    source:
    lib.replaceStrings
      [ "@secret-helper@" "@state-helper@" ]
      [
        "${secretHelper}/bin/homelab-secrets"
        "${requireTermixState}/bin/homelab-termix-state"
      ]
      (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.termix.enable = lib.mkEnableOption "rootless Termix SSH console";

  config = lib.mkIf (config.homelab.termix.enable && servicePrincipal != null && servicePrincipal.name == "termix") {
    homelab.core.imageSources = [ ./containers/termix.container ];
    xdg.configFile."containers/systemd/termix.container".text =
      renderQuadlet ./containers/termix.container;

    home.activation.createTermixState = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg termixState}
    '';
  };
}
