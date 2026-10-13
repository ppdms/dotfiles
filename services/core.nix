{
  config,
  lib,
  pkgs,
  systemConfig,
  profileRoot,
  imageFeed,
  servicePrincipal ? null,
  servicePrincipals,
  ...
}:

let
  cfg = config.homelab.core;
  deployment = systemConfig.homelab;
  metadata = {
    lan-domain = deployment.lanDomain;
    public-domain = deployment.publicDomain;
    timezone = deployment.timeZone;
    proxy-address = deployment.proxyAddress;
    apps-subnet = deployment.appsSubnet;
    vpn-address = deployment.vpn.address;
    vpn-subnet = deployment.vpn.subnet;
    vpn-dns = deployment.vpn.dns;
    vpn-countries = lib.concatStringsSep "," deployment.vpn.countries;
    vpn-gateway = deployment.vpn.gateway;
    raid-root = deployment.paths.raidRoot;
    immich-path = deployment.paths.immich;
    opencloud-path = deployment.paths.opencloud;
    media-path = deployment.paths.media;
    books-path = deployment.paths.books;
    notes-path = deployment.paths.notes;
    config-checkout = deployment.paths.checkout;
    restore-destination = deployment.restoreDestination;
    state-root = servicePrincipal.stateRoot;
    origin-directory = servicePrincipals.origin.directory;
    metrics-directory = servicePrincipals.metricsDirectory;
    host-loopback-address = servicePrincipals.hostLoopbackAddress;
    nginx-state-directory = "${servicePrincipal.home}/.local/state/homelab-nginx";
    ca-certificate = toString profileRoot + "/${deployment.caCertificateFile}";
    instance = deployment.instance;
    display-name = deployment.displayName;
    public-session-name = deployment.publicSessionName;
    metrics-instance = deployment.observability.instance;
    logs-datasource = deployment.observability.logsDatasource;
    metrics-datasource = deployment.observability.metricsDatasource;
    logs-datasource-name = deployment.observability.logsDatasourceName;
    metrics-datasource-name = deployment.observability.metricsDatasourceName;
  }
  // lib.mapAttrs' (name: port: lib.nameValuePair "port-${name}" (toString port)) servicePrincipals.ports
  // lib.mapAttrs' (
    name: port: lib.nameValuePair "backend-${name}" "127.0.0.1:${toString port}"
  ) servicePrincipals.ports
  // lib.optionalAttrs (servicePrincipal.name == "backrest") {
    backup-state-root = deployment.paths.backupState;
    backup-quadlets = deployment.paths.quadletManifests;
  };
  metadataPlaceholders = map (key: "@${key}@") (builtins.attrNames metadata);
  metadataPattern = lib.concatStringsSep "|" metadataPlaceholders;
  metadataValues = map (key: metadata.${key}) (builtins.attrNames metadata);
  renderConfig =
    source:
    let
      # JSON templates place metadata inside strings, including embedded
      # query expressions. Escape replacement contents without adding quotes.
      values =
        if lib.hasSuffix ".json" (toString source) then
          map (value: lib.removeSuffix "\"" (lib.removePrefix "\"" (builtins.toJSON value))) metadataValues
        else
          metadataValues;
      rendered = lib.replaceStrings metadataPlaceholders values (builtins.readFile source);
    in
    assert lib.assertMsg
      # hasInfix's greedy .* expression overflows Nix's regex stack on large
      # dashboard files. Split on the fixed markers instead of matching .*.
      (builtins.length (builtins.split metadataPattern rendered) == 1)
      "Config ${builtins.baseNameOf source} contains unresolved deployment metadata";
    rendered;
  renderConfigDirectory =
    source:
    let
      collect =
        directory:
        let
          children = builtins.readDir directory;
        in
        lib.concatMap (
          name:
          if children.${name} == "directory" then
            map (file: file // { name = "${name}/${file.name}"; }) (collect (directory + "/${name}"))
          else if children.${name} == "regular" then
            [
              {
                inherit name;
                path = directory + "/${name}";
              }
            ]
          else
            throw "Unsupported config entry ${name}"
        ) (builtins.attrNames children);
      files = collect source;
      # Bind-mounted config trees need real files: linkFarm's absolute store
      # symlinks are dangling inside containers that cannot see /nix/store.
    in
    pkgs.runCommand "homelab-${builtins.baseNameOf source}-config" { } ''
      mkdir -p "$out"
      ${lib.concatMapStringsSep "\n" (
        file:
        let
          text = pkgs.writeText "homelab-${builtins.baseNameOf file.path}" (renderConfig file.path);
          mode = if builtins.baseNameOf file.path == "start-nginx.sh" then "755" else "644";
        in
        ''install -D -m ${mode} ${text} "$out"/${lib.escapeShellArg file.name}''
      ) files}
    '';
  # The locked feed owns image references; application modules own their sources.
  placeholders = map (key: "@image:${key}@") (builtins.attrNames cfg.imagePins);
  references = map (key: cfg.imagePins.${key}.reference) (builtins.attrNames cfg.imagePins);
  renderQuadlet =
    source:
    let
      rendered = lib.replaceStrings placeholders references (renderConfig source);
    in
    assert lib.assertMsg (
      !(lib.hasInfix "@image:" rendered)
    ) "Registry Quadlet ${builtins.baseNameOf source} lacks a locked service image pin";
    rendered;
  secrets = pkgs.callPackage ./tools/homelab-secrets.nix {
    lanDomain = deployment.lanDomain;
    publicDomain = deployment.publicDomain;
  };
  imageUpdater = pkgs.callPackage ./tools/image-update.nix {
    inherit secrets imageFeed;
    seed = pkgs.writeText "homelab-image-pins.json" (
      builtins.toJSON {
        schema = 1;
        images = cfg.imagePins;
      }
    );
  };
  resetNetwork = pkgs.writeShellApplication {
    name = "homelab-network-reset";
    runtimeInputs = [ pkgs.podman ];
    text = ''
      # Systemd stops dependent containers first. Never force-remove a bridge:
      # an attachment outside the declared dependency graph must not be deleted.
      if podman network exists "$1"; then
        podman network rm "$1"
      else
        status=$?
        if [[ "$status" -ne 1 ]]; then
          exit "$status"
        fi
      fi
    '';
  };
  renderNetwork =
    source:
    let
      rendered = renderConfig source;
      name = lib.removePrefix "NetworkName=" (
        builtins.head (lib.filter (lib.hasPrefix "NetworkName=") (lib.splitString "\n" rendered))
      );
    in
    rendered
    + ''

      [Service]
      ExecStartPre=${resetNetwork}/bin/homelab-network-reset ${lib.escapeShellArg name}
    '';
  containerUnits = map (name: lib.removeSuffix ".container" (builtins.baseNameOf name) + ".service") (
    builtins.filter (
      name: lib.hasPrefix "containers/systemd/" name && lib.hasSuffix ".container" name
    ) (builtins.attrNames config.xdg.configFile)
  );
in
{
  options.homelab.core = {
    enable = lib.mkEnableOption "account-local rootless homelab infrastructure";
    imageSources = lib.mkOption {
      type = lib.types.listOf lib.types.path;
      default = [ ];
      description = "Registry-backed Quadlet sources contributed by enabled application modules.";
    };
    imagePins = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.submodule {
          options = {
            reference = lib.mkOption {
              type = lib.types.strMatching "[^[:space:]]+@sha256:[0-9a-f]{64}";
              description = "Immutable registry reference rendered verbatim into Quadlet Image= lines.";
            };
            version = lib.mkOption {
              type = lib.types.str;
              description = "Stable release version or selection floor for an unreleased bootstrap image.";
            };
            order = lib.mkOption {
              type = lib.types.listOf lib.types.int;
              default = [ ];
              description = "Stable version/build ordering used to prevent rebuild downgrades.";
            };
            unreleased_baseline = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Keep an existing branch build until a strictly newer stable release is eligible.";
            };
          };
        }
      );
      default = { };
      description = "Pinned service images from the locked service-images feed input.";
    };
    renderConfig = lib.mkOption {
      type = lib.types.functionTo lib.types.str;
      readOnly = true;
      default = renderConfig;
      description = "Render deployment metadata while preserving service helper placeholders.";
    };
    renderConfigDirectory = lib.mkOption {
      type = lib.types.functionTo lib.types.package;
      readOnly = true;
      default = renderConfigDirectory;
      description = "Render a config directory into immutable store files, preserving the nginx launcher executable.";
    };
    renderQuadlet = lib.mkOption {
      type = lib.types.functionTo lib.types.str;
      readOnly = true;
      default = renderQuadlet;
      description = "Render a Quadlet source path, substituting @image:key@ placeholders with pinned references.";
    };
    imageUpdaterPackage = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      default = imageUpdater;
      description = "Image updater bound to the selected private feed locator and locked pins.";
    };
    renderNetwork = lib.mkOption {
      type = lib.types.functionTo lib.types.str;
      readOnly = true;
      default = renderNetwork;
      description = "Render a managed bridge whose start replaces any detached existing network.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = servicePrincipal != null;
        message = "Homelab workload configurations require a dedicated servicePrincipal.";
      }
    ];
    home.packages = lib.optional (cfg.imageSources != [ ] || servicePrincipal.module == "tree-eclass") pkgs.podman;
    homelab.metrics.enable = lib.mkDefault (
      cfg.imageSources != [ ] || servicePrincipal.module == "tree-eclass" || servicePrincipal.name == "metrics"
    );
    xdg.configFile = lib.mkMerge [
      { "containers/policy.json".source = ./containers/policy.json; }
      {
        "containers/systemd/apps-net.network" = lib.mkIf (
          (cfg.imageSources != [ ] || servicePrincipal.module == "tree-eclass")
          && !builtins.elem servicePrincipal.name [ "nginx" "transmission" ]
        ) {
          text = renderNetwork ./containers/apps-net.network;
          onChange = "homelabChangedNetworks+=(apps-net-network.service)";
        };
      }
      # A changed Quadlet (its image pin, mounted config store path, or helper
      # paths) restarts that running container; systemd never does so itself.
      (lib.listToAttrs (
        map (source: {
          name = "containers/systemd/${builtins.baseNameOf source}";
          value.onChange =
            "homelabChangedContainers+=(${lib.removeSuffix ".container" (builtins.baseNameOf source)}.service)";
        }) cfg.imageSources
      ))
    ];
    systemd.user.targets.homelab = {
      Unit.Description = "Homelab ${servicePrincipal.name} services";
      Install.WantedBy = [ "default.target" ];
    };
    home.activation.initializeHomelabChangeTracking = lib.hm.dag.entryBefore [ "onFilesChange" ] ''
      homelabChangedNetworks=()
      homelabChangedContainers=()
    '';
    home.activation.reconcileHomelabNetworks =
      lib.hm.dag.entryAfter [ "reloadSystemd" "onFilesChange" ]
        ''
          if (( ''${#homelabChangedNetworks[@]} )); then
            export XDG_RUNTIME_DIR="''${XDG_RUNTIME_DIR:-/run/user/$(${pkgs.coreutils}/bin/id -u)}"
            case $(${config.systemd.user.systemctlPath} --user is-system-running 2>&1 || true) in
              running|degraded)
                homelabContainerUnits=(${lib.concatMapStringsSep " " lib.escapeShellArg containerUnits})
                homelabActiveContainers=()
                for unit in "''${homelabContainerUnits[@]}"; do
                  case $(${config.systemd.user.systemctlPath} --user show --property=ActiveState --value "$unit") in
                    active|reloading|activating) homelabActiveContainers+=("$unit");;
                  esac
                done
                # Requires/After orders dependent container teardown before bridge
                # deletion. Restore only this account's previously running services.
                run ${config.systemd.user.systemctlPath} --user restart "''${homelabChangedNetworks[@]}"
                if (( ''${#homelabActiveContainers[@]} )); then
                  run ${config.systemd.user.systemctlPath} --user start "''${homelabActiveContainers[@]}"
                fi
                ;;
              *) echo "User systemd daemon not running. Deferring network creation to unit startup.";;
            esac
          fi
        '';
    home.activation.restartChangedHomelabContainers =
      lib.hm.dag.entryBetween [ "startDeclaredHomelabServices" ] [ "reconcileHomelabNetworks" ]
        ''
          if (( ''${#homelabChangedContainers[@]} )); then
            export XDG_RUNTIME_DIR="''${XDG_RUNTIME_DIR:-/run/user/$(${pkgs.coreutils}/bin/id -u)}"
            case $(${config.systemd.user.systemctlPath} --user is-system-running 2>&1 || true) in
              running|degraded)
                # A Quadlet and its drop-in can both change; restart each unit once.
                mapfile -t homelabRestartUnits < <(printf '%s\n' "''${homelabChangedContainers[@]}" | ${pkgs.coreutils}/bin/sort -u)
                # try-restart skips stopped units; the target start below handles those.
                run ${config.systemd.user.systemctlPath} --user try-restart "''${homelabRestartUnits[@]}"
                ;;
              *) echo "User systemd daemon not running. Deferring container restarts to unit startup.";;
            esac
          fi
        '';
    home.activation.startDeclaredHomelabServices =
      lib.hm.dag.entryAfter [ "reconcileHomelabNetworks" ]
        ''
          export XDG_RUNTIME_DIR="''${XDG_RUNTIME_DIR:-/run/user/$(${pkgs.coreutils}/bin/id -u)}"
          case $(${config.systemd.user.systemctlPath} --user is-system-running 2>&1 || true) in
            running|degraded)
              # A dependency may become available after an earlier start job
              # failed. Start this generation's target even when it is active.
              run ${config.systemd.user.systemctlPath} --user start homelab.target
              ;;
            *) echo "User systemd daemon not running. Deferring declared services to unit startup.";;
          esac
        '';
  };
}
