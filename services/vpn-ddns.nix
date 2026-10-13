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
  cfg = config.homelab.vpn-ddns;
  secrets = pkgs.callPackage ./tools/homelab-secrets.nix {
    lanDomain = systemConfig.homelab.lanDomain;
    publicDomain = systemConfig.homelab.publicDomain;
  };
  updater = pkgs.callPackage ./tools/vpn-ddns.nix { inherit secrets; };
in
{
  options.homelab.vpn-ddns = {
    enable = lib.mkEnableOption "Cloudflare dynamic DNS for the host VPN endpoint";
  };

  config = lib.mkIf (cfg.enable && servicePrincipal != null && servicePrincipal.name == "vpn-ddns") {
    home.packages = [ updater ];
    systemd.user.services.vpn-ddns = {
      Unit = {
        Description = "Update the VPN endpoint's public IPv4/IPv6 DNS records";
        PartOf = [ "homelab.target" ];
      };
      Service = {
        Type = "oneshot";
        ExecStart = "${updater}/bin/homelab-vpn-ddns ${
          lib.escapeShellArgs [
            "--zone"
            systemConfig.homelab.dns.zoneId
            "--hostname"
            systemConfig.homelab.dns.vpnHostname
            "--router-interface-id"
            systemConfig.homelab.dns.routerInterfaceId
          ]
        }";
        UMask = "0077";
        NoNewPrivileges = true;
        # iproute2 needs netlink to select the stable host IPv6 source.
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
          "AF_NETLINK"
        ];
        TimeoutStartSec = "55s";
      };
    };
    systemd.user.timers.vpn-ddns = {
      Unit.PartOf = [ "homelab.target" ];
      Timer = {
        OnActiveSec = "5s";
        OnCalendar = "*-*-* *:*:00";
        AccuracySec = "1s";
      };
      Install.WantedBy = [ "homelab.target" ];
    };
  };
}
