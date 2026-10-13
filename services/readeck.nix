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
  readeckState = "${servicePrincipal.stateRoot}/readeck";
  readeckRestore = "${systemConfig.homelab.restoreRoot}/readeck/data";
  requireReadeckState = pkgs.writeShellApplication {
    name = "homelab-readeck-state";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      state=${lib.escapeShellArg readeckState}
      restore=${lib.escapeShellArg readeckRestore}
      install -d -m 0700 "$state"
      # Readeck identity requires config.toml (server secret) and the article
      # database at data/db.sqlite3 (data_directory=data).
      # Startup must not create a replacement store.
      if [[ ! -s "$state/data/data/db.sqlite3" ]]; then
        if [[ ! -s "$restore/data/db.sqlite3" ]]; then
          echo "Readeck start blocked: db.sqlite3 missing under both $state/data/data and $restore/data; refusing a new store." >&2
          exit 1
        fi
        install -d -m 0700 "$state/data"
        cp -a --no-preserve=ownership "$restore/." "$state/data/"
      fi
      if [[ ! -s "$state/data/config.toml" ]]; then
        echo "Readeck start blocked: config.toml server identity is missing; refusing replacement secrets." >&2
        exit 1
      fi
    '';
  };
  renderQuadlet =
    source:
    lib.replaceStrings
      [ "@secret-helper@" "@state-helper@" ]
      [
        "${secretHelper}/bin/homelab-secrets"
        "${requireReadeckState}/bin/homelab-readeck-state"
      ]
      (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.readeck.enable = lib.mkEnableOption "rootless Readeck read-it-later service";

  config = lib.mkIf (config.homelab.readeck.enable && servicePrincipal != null && servicePrincipal.name == "readeck") {
    homelab.core.imageSources = [ ./containers/readeck.container ];
    xdg.configFile."containers/systemd/readeck.container".text =
      renderQuadlet ./containers/readeck.container;

    home.activation.createReadeckState = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg readeckState}
    '';
  };
}
