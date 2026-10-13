{
  config,
  lib,
  pkgs,
  systemConfig,

  servicePrincipal,
  ...
}:

let
  updater = config.homelab.core.imageUpdaterPackage;
  services = map (
    source:
    let
      lines = lib.splitString "\n" (config.homelab.core.renderQuadlet source);
      field =
        name: lib.removePrefix "${name}=" (builtins.head (lib.filter (lib.hasPrefix "${name}=") lines));
    in
    {
      principal = servicePrincipal.name;
      unit = lib.removeSuffix ".container" (builtins.baseNameOf source) + ".service";
      name = field "ContainerName";
      image = field "Image";
    }
  ) config.homelab.core.imageSources;
  specification = pkgs.writeText "homelab-service-images.json" (builtins.toJSON services);
in
{
  config = lib.mkIf config.homelab.core.enable {
    home.packages = lib.optional (servicePrincipal.name == "image-updates") updater;
    # The input repository must already exist to evaluate the flake on a new
    # host; this idempotent activation preserves it on subsequent switches.
    home.activation.initializeImageFeed = lib.mkIf (servicePrincipal.name == "image-updates")
      (lib.hm.dag.entryBetween [ "reloadSystemd" ] [ "writeBoundary" ]
        ''
          run ${updater}/bin/homelab-image-update init
        '');
    home.activation.reconcileHomelabImages = lib.mkIf (services != []) (lib.hm.dag.entryAfter [ "reconcileHomelabNetworks" ] ''
      run ${updater}/bin/homelab-image-update activate --spec ${specification}
    '');
    systemd.user.services.homelab-image-observe = lib.mkIf (servicePrincipal.name == "image-updates") {
      Unit = {
        Description = "Observe stable container releases and publish seven-day-aged image pins";
        PartOf = [ "homelab.target" ];
      };
      Service = {
        Type = "oneshot";
        ExecStart = "${updater}/bin/homelab-image-update scan";
        UMask = "0077";
        TimeoutStartSec = "30min";
      };
    };
    systemd.user.timers.homelab-image-observe = lib.mkIf (servicePrincipal.name == "image-updates") {
      Unit.PartOf = [ "homelab.target" ];
      Timer = {
        OnBootSec = "5min";
        OnUnitActiveSec = "1h";
      };
      Install.WantedBy = [ "homelab.target" ];
    };
    systemd.user.services.homelab-tree-image-observe = lib.mkIf (servicePrincipal.name == "image-updates" && systemConfig.homelab.services.tree-eclass) {
      Unit = {
        Description = "Observe upstream Tree build images and their layers";
        PartOf = [ "homelab.target" ];
        ConditionPathExists = "${systemConfig.homelab.paths.treeCheckout}/Dockerfile";
      };
      Service = {
        Type = "oneshot";
        ExecStart = "${updater}/bin/homelab-image-update tree-inputs --dockerfile ${lib.escapeShellArg "${systemConfig.homelab.paths.treeCheckout}/Dockerfile"}";
        UMask = "0077";
        TimeoutStartSec = "10min";
      };
    };
    systemd.user.timers.homelab-tree-image-observe = lib.mkIf (servicePrincipal.name == "image-updates" && systemConfig.homelab.services.tree-eclass) {
      Unit.PartOf = [ "homelab.target" ];
      Timer = {
        OnBootSec = "10min";
        OnCalendar = "daily";
        Persistent = true;
      };
      Install.WantedBy = [ "homelab.target" ];
    };
  };
}
