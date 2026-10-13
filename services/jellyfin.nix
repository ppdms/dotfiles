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
  jellyfinState = "${servicePrincipal.stateRoot}/jellyfin";
  jellyfinRestore = "${systemConfig.homelab.restoreRoot}/jellyfin";
  requireJellyfinState = pkgs.writeShellApplication {
    name = "homelab-jellyfin-state";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      state=${lib.escapeShellArg jellyfinState}
      restore=${lib.escapeShellArg jellyfinRestore}
      install -d -m 0700 "$state"
      # Jellyfin identity consists of the users/library DB, SSO-OIDC plugin
      # config (bound to the canonical configured auth issuer), and media metadata.
      # Startup requires a populated library database.
      if [[ ! -s "$state/config/data/jellyfin.db" ]]; then
        if [[ ! -s "$restore/config/data/jellyfin.db" ]]; then
          echo "Jellyfin start blocked: jellyfin.db missing under both $state/config/data and $restore/config/data; refusing a new library." >&2
          exit 1
        fi
        install -d -m 0700 "$state/config"
        cp -a --no-preserve=ownership "$restore/config/." "$state/config/"
      fi
      # Jellyfin only honors forwarded/request hosts from KnownProxies: nginx
      # reaches the published backend through rootless port forwarding, whose
      # peer is the container's explicitly assigned address. Keep that address
      # stable so the SSO plugin honors each request's own Host (LAN vs tunnel).
      ${pkgs.python3}/bin/python3 - "$state/config/config/network.xml" ${lib.escapeShellArg systemConfig.homelab.proxyAddress} <<'EOF'
      import re, sys
      from pathlib import Path
      p = Path(sys.argv[1])
      if p.exists():
          t = p.read_text()
          want = f"  <KnownProxies>\n    <string>{sys.argv[2]}</string>\n  </KnownProxies>"
          t = re.sub(r"  <KnownProxies>(?:.*?)</KnownProxies>", want, t, flags=re.S)
          t = re.sub(r"  <EnablePublishedServerUriByRequest>false</EnablePublishedServerUriByRequest>",
                     "  <EnablePublishedServerUriByRequest>true</EnablePublishedServerUriByRequest>", t)
          p.write_text(t)
      EOF
      # Create the cache directory when absent; cached content is regenerable.
      if [[ ! -d "$state/cache" ]]; then
        install -d -m 0700 "$state/cache"
      fi
      # Trust the private root CA so the SSO-OIDC plugin can fetch
      # the configured auth issuer discovery. Delivered at runtime, never store.
      runtime="''${XDG_RUNTIME_DIR:?}/homelab-jellyfin"
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
      [ "@secret-helper@" "@state-helper@" ]
      [
        "${secretHelper}/bin/homelab-secrets"
        "${requireJellyfinState}/bin/homelab-jellyfin-state"
      ]
      (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.jellyfin.enable = lib.mkEnableOption "rootless Jellyfin media server with CPU transcode";

  config = lib.mkIf (config.homelab.jellyfin.enable && servicePrincipal != null && servicePrincipal.name == "jellyfin") {
    homelab.core.imageSources = [ ./containers/jellyfin.container ];
    xdg.configFile."containers/systemd/jellyfin.container".text =
      renderQuadlet ./containers/jellyfin.container;

    home.activation.createJellyfinState = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg jellyfinState}
    '';
  };
}
