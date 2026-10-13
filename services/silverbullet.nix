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
  space = systemConfig.homelab.paths.notes;
  kvState = "${servicePrincipal.stateRoot}/silverbullet/kv";
  requireSilverbulletState = pkgs.writeShellApplication {
    name = "homelab-silverbullet-state";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      space=${lib.escapeShellArg space}
      kv=${lib.escapeShellArg kvState}
      install -d -m 0700 "$kv"
      # The space requires a Git-tracked Zettelkasten vault.
      # Startup must not serve an empty or replacement space.
      if [[ ! -s "$space/.git/HEAD" ]]; then
        echo "SilverBullet blocked: $space/.git/HEAD is missing or empty; a Git-tracked Zettelkasten vault is required. Refusing a fresh space." >&2
        exit 1
      fi
    '';
  };
  renderQuadlet =
    source:
    lib.replaceStrings
      [ "@state-helper@" ]
      [ "${requireSilverbulletState}/bin/homelab-silverbullet-state" ]
      (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.silverbullet.enable = lib.mkEnableOption "SilverBullet notes on the Zettelkasten vault";

  config = lib.mkIf (config.homelab.silverbullet.enable && servicePrincipal != null && servicePrincipal.name == "silverbullet") {
    homelab.core.imageSources = [ ./containers/silverbullet.container ];
    xdg.configFile."containers/systemd/silverbullet.container".text =
      renderQuadlet ./containers/silverbullet.container;
    # Built from the maintained client checkout, outside the writable notes view.
    xdg.configFile."containers/systemd/silverbullet.container.d/10-editor-modes.conf" = {
      force = true;
      onChange = "homelabChangedContainers+=(silverbullet.service)";
      text = ''
        [Container]
        Volume=${servicePrincipal.stateRoot}/silverbullet/editor-modes/silverbullet:/silverbullet:ro
        Environment=SB_INDEX_PAGE=Library/Local/index
      '';
    };

    home.activation.createSilverbulletState = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg kvState}
    '';
  };
}
