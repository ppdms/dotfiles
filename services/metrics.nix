{
  config,
  lib,
  pkgs,

  servicePrincipal,
  ...
}:

let
  quadletMetrics = pkgs.callPackage ./tools/quadlet-metrics.nix {};
  smartMetrics = pkgs.callPackage ./tools/smart-metrics.nix {};
in
{
  options.homelab.metrics.enable = lib.mkEnableOption "per-quadlet cgroup metrics for Alloy";

  config = lib.mkIf config.homelab.metrics.enable {
    home.packages = if servicePrincipal.name == "metrics" then [ smartMetrics ] else [ quadletMetrics ];

    systemd.user.services.homelab-quadlet-metrics = lib.mkIf (servicePrincipal.name != "metrics") {
      Unit = {
        Description = "Scrape per-quadlet cgroup memory/CPU into textfile";
        PartOf = [ "homelab.target" ];
      };
      Service = {
        Type = "oneshot";
        ExecStart = "${quadletMetrics}/bin/homelab-quadlet-metrics";
        UMask = "0077";
      };
    };
    systemd.user.timers.homelab-quadlet-metrics = lib.mkIf (servicePrincipal.name != "metrics") {
      Unit.PartOf = [ "homelab.target" ];
      Timer = {
        OnActiveSec = "30s";
        OnUnitActiveSec = "30s";
      };
      Install.WantedBy = [ "homelab.target" ];
    };
    systemd.user.services.homelab-smart-metrics = lib.mkIf (servicePrincipal.name == "metrics") {
      Unit = {
        Description = "Bridge smartctl exporter into homelab_drive textfile";
        PartOf = [ "homelab.target" ];
      };
      Service = {
        Type = "oneshot";
        ExecStart = "${smartMetrics}/bin/homelab-smart-metrics";
        UMask = "0022";
      };
    };
    systemd.user.timers.homelab-smart-metrics = lib.mkIf (servicePrincipal.name == "metrics") {
      Unit.PartOf = [ "homelab.target" ];
      Timer = {
        OnActiveSec = "60s";
        OnUnitActiveSec = "5min";
      };
      Install.WantedBy = [ "homelab.target" ];
    };
  };
}
