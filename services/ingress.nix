{
  config,
  lib,
  pkgs,
  systemConfig,
  servicePrincipal ? null,
  servicePrincipals,
  ...
}:

let
  secretHelper = pkgs.callPackage ./tools/homelab-secrets.nix {
    lanDomain = systemConfig.homelab.lanDomain;
    publicDomain = systemConfig.homelab.publicDomain;
  };
  autheliaState = "${servicePrincipal.stateRoot}/authelia/data";
  requireAutheliaState = pkgs.writeShellApplication {
    name = "homelab-authelia-state";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      state=${lib.escapeShellArg autheliaState}
      install -d -m 0700 "$state"
      if [[ ! -s "$state/db.sqlite3" ]]; then
        echo "Authelia start blocked: db.sqlite3 is missing or empty under $state. Restore the database and any SQLite WAL using a consistent stopped-service copy, with the matching storage-encryption-key in the scoped credential broker. Refusing to create a replacement identity database." >&2
        exit 1
      fi
      runtime="''${XDG_RUNTIME_DIR:?}/homelab-authelia"
      install -d -m 0700 "$runtime"
      install -m 0600 /dev/null "$runtime/healthcheck.env"
    '';
  };
  initializeNginxState = pkgs.writeShellApplication {
    name = "homelab-nginx-state";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      state=${lib.escapeShellArg "${servicePrincipal.home}/.local/state/homelab-nginx"}
      install -d -m 0700 "$state"
      route="$state/tree-upstream.conf"
      if [[ ! -e "$route" && ! -L "$route" ]]; then
        install -m 0600 /dev/null "$route"
      elif [[ ! -f "$route" || -L "$route" ]]; then
        echo "nginx start blocked: $route must be a regular local route include." >&2
        exit 1
      fi
      chmod 0600 "$route"
    '';
  };
  requireNginxOrigin = pkgs.writeShellApplication {
    name = "homelab-nginx-origin";
    text = ''
      ${initializeNginxState}/bin/homelab-nginx-state
      origin=${lib.escapeShellArg servicePrincipals.origin.directory}
      if [[ ! -d "$origin" || ! -w "$origin" || ! -x "$origin" ]]; then
        echo "nginx start blocked: the declared host origin directory $origin is not writable/traversable; refusing a private duplicate." >&2
        exit 1
      fi
    '';
  };
  renderQuadlet = source: lib.replaceStrings
    [ "@secret-helper@" "@state-helper@" "@nginx-state-helper@" "@nginx-config@" "@authelia-config@" ]
    [
      "${secretHelper}/bin/homelab-secrets"
      "${requireAutheliaState}/bin/homelab-authelia-state"
      "${requireNginxOrigin}/bin/homelab-nginx-origin"
      "${config.homelab.core.renderConfigDirectory ./config/ingress/nginx}"
      "${config.homelab.core.renderConfigDirectory ./config/ingress/authelia}"
    ]
    (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.ingress.enable = lib.mkEnableOption "this principal's ingress service";

  config = lib.mkIf (config.homelab.ingress.enable && servicePrincipal != null) (lib.mkMerge [
    (lib.mkIf (servicePrincipal.name == "authelia") {
      homelab.core.imageSources = [ ./containers/authelia.container ];
      homelab.secrets.mutableComponents = [ "authelia" ];
      xdg.configFile."containers/systemd/authelia.container".text =
        renderQuadlet ./containers/authelia.container;
      home.activation.createAutheliaState = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
        run ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg autheliaState}
      '';
    })
    (lib.mkIf (servicePrincipal.name == "nginx") {
      homelab.core.imageSources = [ ./containers/nginx.container ];
      xdg.configFile."containers/systemd/nginx.container".text =
        renderQuadlet ./containers/nginx.container;
      home.activation.createNginxState = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
        run ${initializeNginxState}/bin/homelab-nginx-state
      '';
    })
    (lib.mkIf (servicePrincipal.name == "cloudflared") {
      homelab.core.imageSources = [ ./containers/cloudflared.container ];
      xdg.configFile."containers/systemd/cloudflared.container".text =
        renderQuadlet ./containers/cloudflared.container;
    })
  ]);
}
