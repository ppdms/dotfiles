{
  config,
  lib,
  pkgs,
  utils,
  systemConfig,
  profileRoot,
  imageFeed,
  imagePins,
  servicePrincipals,
  ...
}:
let
  registry = servicePrincipals;
  roles = registry.principals;
  homeRoles = lib.filterAttrs (name: _: name != "credentials") roles;
  deployment = systemConfig.homelab;
  definitions = registry.definitions;
  roleHome = name: definitions.${name}.home;
  # Services built from a local checkout (services/self-built.nix).
  selfBuilt = registry.selfBuilt;
  updaterFeed = "${roleHome "image-updates"}/image-feed";
  roleConfig =
    role:
    systemConfig
    // {
      system = systemConfig.system // {
        username = role.account;
        uid = role.uid;
        homeDirectory = role.home;
        checkout = "${role.home}/checkout";
      };
      homelab = deployment // {
        stateRoot = role.stateRoot;
        restoreRoot = "${role.home}/restores";
        restoreDestination = "${role.home}/restore";
        paths = deployment.paths // {
          immich = "${role.home}/storage/immich";
          opencloud = "${role.home}/storage/opencloud";
          media = "${role.home}/storage/media";
          books = "${role.home}/storage/books";
          notes = "${role.home}/storage/notes";
          checkout = "${role.home}/checkout";
          treeCheckout = "${role.home}/checkout";
          backupState = "${role.home}/backup/state";
          quadletManifests = "${role.home}/backup/quadlets";
        };
      };
    };
  roleHomes = lib.mapAttrs' (
    _: role:
    lib.nameValuePair role.account {
      # Import only the infrastructure every role needs plus this role's
      # own service module. Importing every service for every account made
      # any service edit change (and restart) all two dozen Home Manager
      # generations, and multiplied evaluation work by the account count.
      imports = [
        ../../services/core.nix
        ../../services/secrets.nix
        ../../services/image-updates.nix
        ../../services/metrics.nix
      ]
      ++ lib.optional (role.module != null && role.module != "metrics") ../../services/${role.module}.nix;
      _module.args = {
        systemConfig = roleConfig role;
        servicePrincipal = role;
        imageFeed = updaterFeed;
      };
      home = {
        username = role.account;
        homeDirectory = role.home;
        stateVersion = systemConfig.system.homeStateVersion;
      };
      # Root preparation creates this directory and protects state-view ancestors.
      # Home Manager's empty-directory marker must not mutate those ancestors.
      home.file."${role.home}/.local/state/.keep".enable = false;
      homelab = {
        core = {
          enable = true;
          inherit imagePins;
        };
        metrics.enable =
          deployment.services.metrics
          && !builtins.elem role.name [
            "image-updates"
            "image-builds"
            "secrets-export"
            "vpn-ddns"
          ];
      }
      # Only the imported module's option exists; unimported services
      # keep their mkEnableOption default (false), as before.
      // lib.optionalAttrs (role.module != null && role.module != "metrics") {
        ${role.module}.enable = true;
      };
      xdg.configFile."containers/containers.conf".text = ''
        [network]
        pasta_options = ["--map-host-loopback", "${registry.hostLoopbackAddress}"]
      '';
      systemd.user.startServices = "sd-switch";
    }
  ) homeRoles;
  homes = lib.mapAttrs (_: role: config.home-manager.users.${role.account}) homeRoles;
  serviceImages = lib.concatLists (
    lib.mapAttrsToList (
      name: home:
      map (
        source:
        let
          lines = lib.splitString "\n" (home.homelab.core.renderQuadlet source);
          field =
            key: lib.removePrefix "${key}=" (builtins.head (lib.filter (lib.hasPrefix "${key}=") lines));
        in
        {
          principal = name;
          unit = "${lib.removeSuffix ".container" (builtins.baseNameOf source)}.service";
          name = field "ContainerName";
          image = field "Image";
        }
      ) home.homelab.core.imageSources
    ) homes
  );
  imageSpec = pkgs.writeText "homelab-runtime-images.json" (builtins.toJSON serviceImages);
  signaturePolicy = ../../services/containers/policy.json;
  manifestFiles = lib.concatLists (
    lib.mapAttrsToList (
      name: home:
      lib.mapAttrsToList (path: file: {
        target = "${name}/${lib.removePrefix "containers/systemd/" path}";
        source = file.source;
      }) (lib.filterAttrs (path: _: lib.hasPrefix "containers/systemd/" path) home.xdg.configFile)
    ) homes
  );
  quadletManifests = pkgs.runCommand "homelab-all-role-quadlets" { } ''
    mkdir -p "$out"
    ${lib.concatMapStringsSep "\n" (
      file:
      ''install -D -m 0444 ${lib.escapeShellArg (toString file.source)} "$out"/${lib.escapeShellArg file.target}''
    ) manifestFiles}
  '';
  view =
    principal: source: target: writable: extra:
    {
      inherit
        principal
        source
        target
        writable
        ;
    }
    // extra;
  storageMount =
    source:
    lib.foldl' (
      selected: mount:
      if
        (source == mount || lib.hasPrefix "${mount}/" source)
        && builtins.stringLength mount > builtins.stringLength selected
      then
        mount
      else
        selected
    ) "/" (builtins.attrNames systemConfig.hardware.fileSystems);
  storageView =
    principal: key: writable:
    view principal deployment.paths.${key} "${roleHome principal}/storage/${key}" writable {
      requireMount =
        let
          mount = storageMount deployment.paths.${key};
        in
        if mount == "/" then null else mount;
    };
  restoreViews =
    lib.concatMap
      (
        entry:
        lib.optional (builtins.hasAttr entry.principal roles) (
          view entry.principal "${deployment.restoreRoot}/${entry.leaf}"
            "${roleHome entry.principal}/restores/${entry.leaf}"
            false
            { optional = true; }
        )
      )
      [
        {
          principal = "homeassistant";
          leaf = "homeAssistant";
        }
        {
          principal = "jellyfin";
          leaf = "jellyfin";
        }
        {
          principal = "readeck";
          leaf = "readeck";
        }
        {
          principal = "termix";
          leaf = "termix";
        }
        {
          principal = "transmission";
          leaf = "transmission";
        }
      ];
  views =
    lib.concatMap
      (
        entry:
        lib.optional (builtins.hasAttr entry.principal roles) (
          storageView entry.principal entry.key entry.writable
        )
      )
      [
        {
          principal = "immich";
          key = "immich";
          writable = false;
        }
        {
          principal = "opencloud";
          key = "opencloud";
          writable = true;
        }
        {
          principal = "jellyfin";
          key = "media";
          writable = false;
        }
        {
          principal = "kavita";
          key = "books";
          writable = false;
        }
        {
          principal = "silverbullet";
          key = "notes";
          writable = true;
        }
        {
          principal = "transmission";
          key = "media";
          writable = true;
        }
        {
          principal = "backrest";
          key = "immich";
          writable = false;
        }
        {
          principal = "backrest";
          key = "opencloud";
          writable = false;
        }
      ]
    ++ restoreViews
    ++ lib.optionals (builtins.hasAttr "image-updates" roles) (
      [
        (view "image-updates" "${registry.operator.homeDirectory}/.local/state/homelab-image-updates"
          "${roleHome "image-updates"}/.local/state/homelab-image-updates"
          true
          { }
        )
      ]
      ++ lib.optional deployment.services.tree-eclass (
        view "image-updates" deployment.paths.treeCheckout "${roleHome "image-updates"}/checkout" false { }
      )
      # Each self-built checkout is visible read-only so its Dockerfile's base
      # images are observed on a timer. A missing checkout is skipped, not fatal.
      ++ lib.mapAttrsToList (
        name: image:
        view "image-updates" image.checkout "${roleHome "image-updates"}/checkouts/${name}" false {
          optional = true;
        }
      ) selfBuilt
    )
    ++ lib.optionals (builtins.hasAttr "backrest" roles) [
      (view "backrest" deployment.paths.checkout "${roleHome "backrest"}/checkout" false { })
      (view "backrest" deployment.restoreDestination "${roleHome "backrest"}/restore" true {
        optional = true;
      })
    ];
  principalManifest = registry // {
    statePreparation = {
      inherit views;
      haSecrets = "${registry.physicalStateRoot}/homeassistant/config/secrets.yaml";
      forbiddenSources = [ "${registry.operator.homeDirectory}/.config/keys" ];
      imageFeed = {
        source = imageFeed;
        target = updaterFeed;
      };
      originalUnits = lib.unique (
        lib.concatMap (role: role.units) (builtins.attrValues homeRoles)
        ++ [
          "homelab-secrets-persist.service"
          "homelab-secrets-persist.timer"
          "homelab-secrets-export.service"
          "homelab-secrets-export.timer"
          "homelab-image-observe.service"
          "homelab-image-observe.timer"
          "homelab-tree-image-observe.service"
          "homelab-tree-image-observe.timer"
          "homelab-quadlet-metrics.service"
          "homelab-quadlet-metrics.timer"
          "homelab-smart-metrics.service"
          "homelab-smart-metrics.timer"
          "vpn-ddns.service"
          "vpn-ddns.timer"
        ]
      );
      originalTreeImages = lib.optionals deployment.services.tree-eclass [
        {
          principal = "tree-eclass";
          image = "localhost/tree-eclass:stable";
        }
        {
          principal = "tree-dev";
          image = "localhost/tree-eclass:dev";
        }
      ];
      backupViews = lib.optionals (builtins.hasAttr "backrest" roles) [
        {
          source = registry.physicalStateRoot;
          target = "${roleHome "backrest"}/backup/state";
        }
        {
          source = toString quadletManifests;
          target = "${roleHome "backrest"}/backup/quadlets";
        }
      ];
    }
    // lib.optionalAttrs deployment.services.tree-eclass {
      treeDev = {
        sessionSource = "${registry.physicalStateRoot}/tree-eclass";
        sessionTarget = "${definitions.tree-dev.stateRoot}/tree-eclass-dev";
        checkoutSource = deployment.paths.treeCheckout;
        checkoutTarget = "${roleHome "tree-dev"}/checkout";
      };
    };
  };
  secrets = pkgs.callPackage ../../services/tools/homelab-secrets.nix {
    inherit (deployment) lanDomain publicDomain;
  };
  state = pkgs.callPackage ../../services/tools/service-state.nix { inherit secrets; };
  broker = pkgs.callPackage ../../services/tools/credential-broker.nix { inherit secrets; };
  images = pkgs.callPackage ../../services/tools/image-update.nix {
    inherit secrets;
    imageFeed = updaterFeed;
    seed = pkgs.writeText "homelab-approved-image-pins.json" (
      builtins.toJSON {
        schema = 1;
        images = imagePins;
      }
    );
  };
  repository = pkgs.writeShellApplication {
    name = "homelab-backrest-repository";
    text = ''
      export PYTHONPATH=${secrets}/share/homelab
      export HOMELAB_REPOSITORY_URI=${lib.escapeShellArg deployment.backup.repositoryUri}
      export HOMELAB_REPOSITORY_ID=${lib.escapeShellArg deployment.backup.repositoryId}
      export HOMELAB_PODMAN=${pkgs.podman}/bin/podman
      export HOMELAB_ENV=${pkgs.coreutils}/bin/env
      export HOMELAB_SUDO=/run/wrappers/bin/sudo
      export HOMELAB_PYTHON=${secrets.python}/bin/python3
      export HOMELAB_SECRETS=${secrets}/bin/homelab-secrets
      exec ${secrets.python}/bin/python3 ${../../services/tools/backrest-repository.py} "$@"
    '';
  };
  provision = pkgs.callPackage ../../services/tools/backrest-provision.nix {
    inherit
      secrets
      images
      imageSpec
      signaturePolicy
      repository
      ;
  };
  creds = pkgs.callPackage ../../services/tools/creds.nix { inherit secrets; };
  tern = pkgs.callPackage ../../programs/tern/tool.nix {
    inherit secrets;
    checkout = systemConfig.system.checkout;
  };
  treeDev = pkgs.callPackage ../../services/tools/tree-dev.nix {
    python3 = secrets.python;
    imageUpdate = images;
    treeCheckout = deployment.paths.treeCheckout;
  };
  imageBuildJournal = "/var/lib/homelab/image-builds";
  imageBuildSpec = pkgs.writeText "homelab-image-builds.json" (
    builtins.toJSON {
      schema = 1;
      builder = "image-builds";
      recovery = imageBuildJournal;
      images = lib.mapAttrs (
        _: image:
        {
          inherit (image)
            repository
            checkout
            dockerfile
            principal
            unit
            container
            serve
            revisionBuildArg
            ;
        }
      ) selfBuilt;
    }
  );
  imageBuild = pkgs.callPackage ../../services/tools/image-build.nix {
    python3 = secrets.python;
    imageUpdate = images;
    buildSpec = imageBuildSpec;
  };
  runtimeUnits = map (role: "user-runtime-dir@${toString role.uid}.service") (
    builtins.attrValues roles
  );
  originalRuntimeUnit = "user-runtime-dir@${toString registry.operator.uid}.service";
  prerequisites = [
    "homelab-service-state.service"
    "homelab-credentials-initialize.service"
    "homelab-service-state-protect.service"
    "homelab-runtime-images.service"
  ];
  rolePrerequisites =
    role: prerequisites ++ lib.optional (role.name == "backrest") "homelab-backup-access.service";
  # Wants/After orders startup without tying existing managers to provider stops.
  # Check each unit separately: is-active with several units succeeds if any is active.
  activeChecks =
    units:
    map (
      unit: "${pkgs.systemd}/bin/systemctl --system is-active --quiet ${lib.escapeShellArg unit}"
    ) units;
  credentialsRole = definitions.credentials;
  enabled = builtins.hasAttr "credentials" roles;
in
{
  assertions = [
    {
      assertion = homeRoles == { } || enabled;
      message = "Dedicated homelab workloads require the core credential/state infrastructure.";
    }
  ];
  users.groups =
    lib.mapAttrs' (_: role: lib.nameValuePair role.account { gid = role.gid; }) roles
    // lib.optionalAttrs deployment.services.ingress {
      ${registry.origin.group}.gid = registry.origin.gid;
    };
  users.users = lib.mapAttrs' (
    name: role:
    lib.nameValuePair role.account {
      isSystemUser = true;
      uid = role.uid;
      group = role.account;
      home = role.home;
      createHome = true;
      homeMode = "0700";
      shell = "${pkgs.shadow}/bin/nologin";
      linger = true;
      subUidRanges = [
        {
          startUid = role.subUidStart;
          count = role.subIdCount;
        }
      ];
      subGidRanges = [
        {
          startGid = role.subGidStart;
          count = role.subIdCount;
        }
      ];
      extraGroups =
        lib.optional (builtins.elem name [
          "nginx"
          "cloudflared"
        ]) registry.origin.group
        ++ lib.optional (name == "alloy") "systemd-journal";
    }
  ) roles;
  systemd.tmpfiles.rules = [
    "d /var/lib/homelab 0711 root root -"
    "d /var/lib/homelab/users 0711 root root -"
    "d ${registry.metricsDirectory} 0755 root root -"
  ]
  ++ lib.mapAttrsToList (
    name: role: "d ${registry.metricsDirectory}/${name} 0755 ${role.account} ${role.account} -"
  ) homeRoles
  ++ lib.optionals deployment.services.tree-eclass [
    "d /var/lib/homelab/tree-releases 0755 root root -"
  ]
  ++ lib.optionals (selfBuilt != { }) [
    "d ${imageBuildJournal} 0700 root root -"
  ]
  ++ lib.optionals deployment.services.ingress [
    "d ${registry.origin.directory} 2750 ${definitions.nginx.account} ${registry.origin.group} -"
  ];
  environment.etc."homelab/principals.json".text = builtins.toJSON principalManifest;
  environment.etc."homelab/runtime-images.json".source = imageSpec;
  environment.etc."homelab/quadlet-manifests".source = quadletManifests;
  home-manager.users = roleHomes // {
    ${systemConfig.system.username} = {
      _module.args = {
        inherit systemConfig imageFeed;
        servicePrincipal = null;
      };
      home.packages = [
        secrets
        broker
        creds
        tern
        images
        provision
        repository
      ]
      ++ lib.optional deployment.services.tree-eclass treeDev
      ++ lib.optional (selfBuilt != { }) imageBuild;
      programs.git.settings.safe.directory = [ imageFeed ];
    };
  };
  system.build = {
    homelabPrincipals = pkgs.writeText "homelab-principals.json" (builtins.toJSON principalManifest);
    homelabImageSpec = imageSpec;
    homelabQuadletManifests = quadletManifests;
    homelabCredentialBroker = broker;
    homelabServiceState = state;
    homelabImageUpdate = images;
    homelabBackrestProvision = provision;
    homelabBackrestRepository = repository;
    homelabCreds = creds;
    homelabTern = tern;
    homelabTreeDev = treeDev;
    homelabImageBuild = imageBuild;
  };
  systemd.services = lib.mkMerge [
    (lib.mkIf enabled {
      "home-manager-${utils.escapeSystemdPath registry.operator.username}" = {
        requires = prerequisites;
        after = prerequisites;
      };
      homelab-service-state = {
        description = "Prepare dedicated homelab dataset and runtime views";
        wantedBy = [ "multi-user.target" ];
        wants = runtimeUnits ++ [ originalRuntimeUnit ];
        after = [
          "systemd-tmpfiles-setup.service"
          originalRuntimeUnit
        ]
        ++ runtimeUnits;
        before = [
          "homelab-credentials.service"
          "homelab-runtime-images.service"
        ];
        restartTriggers = [ config.environment.etc."homelab/principals.json".source ];
        unitConfig.RequiresMountsFor = [
          registry.physicalStateRoot
          deployment.paths.raidRoot
          imageFeed
        ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStartPre = activeChecks (runtimeUnits ++ [ originalRuntimeUnit ]);
          ExecStart = "${state}/bin/homelab-service-state prepare";
          TimeoutStartSec = "infinity";
          UMask = "0077";
        };
      };
      homelab-credentials = {
        description = "UID-scoped TPM-bound homelab credential broker";
        wantedBy = [ "multi-user.target" ];
        requires = [
          "homelab-service-state.service"
          "user-runtime-dir@${toString credentialsRole.uid}.service"
        ];
        after = [
          "homelab-service-state.service"
          "user-runtime-dir@${toString credentialsRole.uid}.service"
        ];
        environment = {
          HOME = credentialsRole.home;
          XDG_RUNTIME_DIR = "/run/user/${toString credentialsRole.uid}";
        };
        serviceConfig = {
          Type = "notify";
          NotifyAccess = "main";
          User = credentialsRole.account;
          Group = credentialsRole.account;
          ExecStart = "${broker}/bin/homelab-credential-broker serve";
          RuntimeDirectory = "homelab-credentials";
          RuntimeDirectoryMode = "0755";
          StateDirectory = "homelab-credentials";
          StateDirectoryMode = "0700";
          UMask = "0077";
          Restart = "on-failure";
          RestartSec = "2s";
          TimeoutStartSec = "30min";
          NoNewPrivileges = true;
        };
      };
      homelab-credentials-initialize = {
        description = "Initialize encrypted credential seeds through the scoped broker";
        wantedBy = [ "multi-user.target" ];
        requires = [ "homelab-credentials.service" ];
        after = [ "homelab-credentials.service" ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${broker}/bin/homelab-credential-broker initialize";
          TimeoutStartSec = "30min";
          UMask = "0077";
        };
      };
      homelab-service-state-protect = {
        description = "Protect the verified Home Assistant secret source";
        wantedBy = [ "multi-user.target" ];
        requires = [
          "homelab-service-state.service"
          "homelab-credentials-initialize.service"
        ];
        after = [
          "homelab-service-state.service"
          "homelab-credentials-initialize.service"
        ];
        restartTriggers = [ config.environment.etc."homelab/principals.json".source ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${state}/bin/homelab-service-state protect";
          TimeoutStartSec = "30min";
          UMask = "0077";
        };
      };
      homelab-backup-access = {
        description = "Reconcile scoped read-only Backrest dataset access";
        wantedBy = lib.optional (builtins.hasAttr "backrest" roles) "multi-user.target";
        requires = [ "homelab-service-state.service" ];
        after = [ "homelab-service-state.service" ];
        restartTriggers = [ config.environment.etc."homelab/principals.json".source ];
        unitConfig.RequiresMountsFor = [
          registry.physicalStateRoot
          deployment.paths.raidRoot
        ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${state}/bin/homelab-service-state backup-access";
          TimeoutStartSec = "infinity";
          UMask = "0077";
        };
      };
      homelab-runtime-images = {
        description = "Distribute approved immutable images into dedicated rootless stores";
        wantedBy = [ "multi-user.target" ];
        requires = [
          "homelab-service-state.service"
          "homelab-credentials-initialize.service"
        ];
        wants = runtimeUnits;
        after = [
          "homelab-service-state.service"
          "homelab-credentials-initialize.service"
        ]
        ++ runtimeUnits;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStartPre = activeChecks runtimeUnits;
          ExecStart = "${images}/bin/homelab-image-update distribute --spec ${imageSpec} --signature-policy ${signaturePolicy}";
          TimeoutStartSec = "1h";
          UMask = "0077";
        };
      };
    })
    (lib.mapAttrs' (
      _: role:
      lib.nameValuePair "home-manager-${utils.escapeSystemdPath role.account}" {
        requires = rolePrerequisites role ++ [
          "user@${toString role.uid}.service"
          "user-runtime-dir@${toString role.uid}.service"
        ];
        after = rolePrerequisites role ++ [
          "user@${toString role.uid}.service"
          "user-runtime-dir@${toString role.uid}.service"
        ];
        serviceConfig.TimeoutStartSec = lib.mkForce "30min";
      }
    ) homeRoles)
    (lib.mapAttrs' (
      _: role:
      lib.nameValuePair "user@${toString role.uid}" {
        overrideStrategy = "asDropin";
        wants = rolePrerequisites role;
        after = rolePrerequisites role;
        serviceConfig.ExecStartPre = activeChecks (rolePrerequisites role);
      }
    ) homeRoles)
  ];
}
