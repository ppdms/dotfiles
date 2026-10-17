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
  selfBuilt = import ./self-built.nix { inherit lib systemConfig; };
  # Dockerfiles of every locally built image, as the updater sees them: each
  # checkout is a read-only view (machines/server/accounts.nix), so the
  # upstream base images can be observed for the 7-day quarantine without
  # anyone running a command.
  buildInputs =
    lib.optional systemConfig.homelab.services.tree-eclass {
      name = "tree-eclass";
      dockerfile = "${systemConfig.homelab.paths.treeCheckout}/Dockerfile";
    }
    ++ lib.mapAttrsToList (name: image: {
      inherit name;
      dockerfile = "${servicePrincipal.home}/checkouts/${name}/${image.dockerfile}";
    }) selfBuilt;
  buildInputsSpecification = pkgs.writeText "homelab-build-inputs.json" (
    builtins.toJSON {
      schema = 1;
      images = buildInputs;
    }
  );
  observesBuilds = servicePrincipal.name == "image-updates" && buildInputs != [ ];
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
    systemd.user.services.homelab-build-inputs-observe = lib.mkIf observesBuilds {
      Unit = {
        Description = "Observe the upstream base images of locally built service images";
        PartOf = [ "homelab.target" ];
      };
      Service = {
        Type = "oneshot";
        ExecStart = "${updater}/bin/homelab-image-update observe-builds --spec ${buildInputsSpecification}";
        UMask = "0077";
        TimeoutStartSec = "10min";
      };
    };
    systemd.user.timers.homelab-build-inputs-observe = lib.mkIf observesBuilds {
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
