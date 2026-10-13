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
  transmissionState = "${servicePrincipal.stateRoot}/transmission";
  transmissionRestore = "${systemConfig.homelab.restoreRoot}/transmission/transmission";
  requireTransmissionState = pkgs.writeShellApplication {
    name = "homelab-transmission-state";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      state=${lib.escapeShellArg transmissionState}
      restore=${lib.escapeShellArg transmissionRestore}
      install -d -m 0700 "$state"
      # Transmission requires daemon configuration (transmission-home) and the
      # Proton WireGuard directory (servers cache, resolv.conf).
      # Startup must not create a replacement store.
      if [[ ! -s "$state/transmission-home/settings.json" ]]; then
        if [[ ! -s "$restore/transmission-home/settings.json" ]]; then
          echo "Transmission start blocked: settings.json missing under both $state/transmission-home and $restore/transmission-home; refusing a new store." >&2
          exit 1
        fi
        install -d -m 0700 "$state/transmission-home" "$state/protonvpn"
        cp -a --no-preserve=ownership "$restore/transmission-home/." "$state/transmission-home/"
        cp -a --no-preserve=ownership "$restore/protonvpn/." "$state/protonvpn/"
        # Drop stale runtime files from the snapshot; the live tunnel
        # recreates them. Keep servers/ cache and resolv.conf.
        rm -f "$state/protonvpn/forwarded_port" "$state/protonvpn/ip"
      fi
      if [[ ! -s "$state/protonvpn/resolv.conf" ]]; then
        echo "Transmission start blocked: Proton VPN state/resolv.conf is missing; refusing a replacement tunnel store." >&2
        exit 1
      fi
      # Gluetun reads the private key from env, not *_FILE. Assemble the
      # Quadlet EnvironmentFile from this role's scoped runtime credential.
      secrets="''${XDG_RUNTIME_DIR:?}/homelab-secrets/transmission"
      {
        printf 'WIREGUARD_PRIVATE_KEY=%s\n' "$(cat "$secrets/wireguard-private-key")"
      } > "$secrets/gluetun.env"
      chmod 0600 "$secrets/gluetun.env"
    '';
  };
  renderQuadlet =
    source:
    lib.replaceStrings
      [ "@secret-helper@" "@state-helper@" "@transmission-config@" ]
      [
        "${secretHelper}/bin/homelab-secrets"
        "${requireTransmissionState}/bin/homelab-transmission-state"
        "${config.homelab.core.renderConfigDirectory ./config/transmission}"
      ]
      (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.transmission.enable = lib.mkEnableOption "rootless Transmission daemon over WireGuard tunnel with paused start";

  config = lib.mkIf (config.homelab.transmission.enable && servicePrincipal != null && servicePrincipal.name == "transmission") {
    homelab.core.imageSources = [
      ./containers/transmission-gluetun.container
      ./containers/transmission.container
    ];
    xdg.configFile."containers/systemd/transmission-gluetun.container".text =
      renderQuadlet ./containers/transmission-gluetun.container;
    xdg.configFile."containers/systemd/transmission.container".text =
      renderQuadlet ./containers/transmission.container;
    xdg.configFile."containers/systemd/transmission-data.volume".text =
      config.homelab.core.renderConfig ./containers/transmission-data.volume;
    xdg.configFile."containers/systemd/transmission.network" = {
      text = config.homelab.core.renderNetwork ./containers/transmission.network;
      onChange = "homelabChangedNetworks+=(transmission-network.service)";
    };

    home.activation.createTransmissionState = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg transmissionState}
    '';
  };
}
