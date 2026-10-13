{
  config,
  lib,
  pkgs,
  systemConfig,
  profileRoot,
  approvedDeployment,
  servicePrincipals,
  ...
}:
let
  deployment = systemConfig.homelab;
  network = systemConfig.network;
  username = systemConfig.system.username;
  approvalRuntime = pkgs.callPackage ../../services/tools/approved-deployment-runtime.nix { };
  forwards = lib.optionalAttrs deployment.services.ingress {
    http = {
      listen = 80;
      destination = servicePrincipals.ports.nginxHttp;
    };
    https = {
      listen = 443;
      destination = servicePrincipals.ports.nginxHttps;
    };
  };
  firewallRule = "-i ${network.lanInterface} -s ${network.lanSubnet} -d ${network.lanAddress} -p tcp -m multiport --dports 80,443 -j nixos-fw-accept";
  privateBackends = lib.mapAttrsToList (name: role: {
    inherit name;
    port = servicePrincipals.ports.${name};
    allowed = lib.unique [
      0
      systemConfig.system.uid
      servicePrincipals.definitions.nginx.uid
      role.uid
    ];
  }) (lib.filterAttrs (name: _: builtins.hasAttr name servicePrincipals.ports) servicePrincipals.principals)
  # Tern's web server is an operator-owned process, not a service principal, so
  # it needs its own fence: only nginx (plus root and the operator) may connect.
  ++ lib.optional deployment.services.ingress {
    name = "omp";
    port = servicePrincipals.ports.omp;
    allowed = lib.unique [
      0
      systemConfig.system.uid
      servicePrincipals.definitions.nginx.uid
    ];
  };
  iptables = "${pkgs.iptables}/bin/iptables";
  backendRules = lib.concatMapStringsSep "\n" (backend:
    lib.concatMapStringsSep "\n" (uid:
      "${iptables} -A homelab-private -p tcp --dport ${toString backend.port} -m owner --uid-owner ${toString uid} -j RETURN"
    ) backend.allowed + "\n"
    + "${iptables} -A homelab-private -p tcp --dport ${toString backend.port} -j REJECT --reject-with tcp-reset"
  ) privateBackends;
  removeBackendRules = ''
    while ${iptables} -D OUTPUT -d 127.0.0.0/8 -p tcp -j homelab-private 2>/dev/null; do :; done
    ${iptables} -F homelab-private 2>/dev/null || true
    ${iptables} -X homelab-private 2>/dev/null || true
  '';
in
{
  boot.swraid.enable = systemConfig.hardware.raidEnabled;
  boot.swraid.mdadmConf = lib.mkIf systemConfig.hardware.raidEnabled "PROGRAM ${pkgs.coreutils}/bin/true";

  services.prometheus.exporters.node = {
    enable = deployment.services.metrics;
    listenAddress = "127.0.0.1";
    port = 9100;
    enabledCollectors = [ "systemd" ];
  };
  services.prometheus.exporters.process = {
    enable = deployment.services.metrics;
    listenAddress = "127.0.0.1";
    port = 9256;
    settings.process_names = [
      {
        name = "{{.Matches.Group}}";
        cmdline = [ "conmon.*--name (?P<Group>[^- ]+).*" ];
      }
      {
        name = "{{.Matches.Group}}";
        cmdline = [ "/usr/bin/podman.*--name (?P<Group>[^- ]+).*" ];
      }
    ];
  };
  services.prometheus.exporters.smartctl = {
    enable = deployment.services.metrics;
    listenAddress = "127.0.0.1";
    port = 9633;
    devices = systemConfig.hardware.smartDevices;
  };
  virtualisation.podman.enable = true;
  virtualisation.podman.autoPrune.enable = false;
  systemd.user.sockets.podman.wantedBy = lib.mkForce [ ];
  security.unprivilegedUsernsClone = true;
  users.users.${username} = {
    linger = true;
    subUidRanges = systemConfig.system.subUidRanges;
    subGidRanges = systemConfig.system.subGidRanges;
  };
  # A manual switch is approval of the reviewed immutable configuration.
  security.sudo.wheelNeedsPassword = true;
  security.pki.certificateFiles = [ (profileRoot + "/${deployment.caCertificateFile}") ];
  system.build.approvedDeployment = approvedDeployment;
  system.build.approvedDeploymentRuntime = approvalRuntime;
  system.extraDependencies = [ approvedDeployment ];
  system.activationScripts.approvedDeployment = {
    deps = [ "users" ];
    text = ''
      ${approvalRuntime}/bin/homelab-approved-deployment install ${approvedDeployment} || exit 1
    '';
  };

  networking.firewall = lib.mkMerge [
    (lib.mkIf (config.networking.firewall.backend == "iptables") {
      extraCommands = ''
        ${iptables} -I nixos-fw 1 ${firewallRule}
        ${lib.optionalString (privateBackends != []) ''
          ${removeBackendRules}
          ${iptables} -N homelab-private
          ${backendRules}
          ${iptables} -I OUTPUT 1 -d 127.0.0.0/8 -p tcp -j homelab-private
        ''}
      '';
      extraStopCommands = ''
        ${iptables} -D nixos-fw ${firewallRule} 2>/dev/null || true
        ${removeBackendRules}
      '';
    })
    (lib.mkIf (config.networking.firewall.backend == "nftables") {
      extraInputRules = ''iifname "${network.lanInterface}" ip saddr ${network.lanSubnet} ip daddr ${network.lanAddress} tcp dport { 80, 443 } accept'';
    })
  ];
  networking.nftables.tables.homelab-private = lib.mkIf (
    config.networking.firewall.enable
    && config.networking.firewall.backend == "nftables"
    && privateBackends != []
  ) {
    family = "inet";
    content = ''
      chain output {
        type filter hook output priority filter; policy accept;
        ${lib.concatMapStringsSep "\n" (backend: ''
          ip daddr 127.0.0.0/8 tcp dport ${toString backend.port} meta skuid != { ${lib.concatMapStringsSep ", " toString backend.allowed} } reject with tcp reset
        '') privateBackends}
      }
    '';
  };
  systemd.sockets =
    lib.mapAttrs' (
      name: ports:
      lib.nameValuePair "homelab-ingress-${name}" {
        description = "LAN-only ingress ${name} forwarding socket";
        wantedBy = [ "sockets.target" ];
        socketConfig = {
          ListenStream = "${network.lanAddress}:${toString ports.listen}";
          BindToDevice = network.lanInterface;
          FreeBind = true;
          IPAddressDeny = "any";
          IPAddressAllow = [
            network.lanSubnet
            "localhost"
          ];
        };
      }
    ) forwards
    // lib.optionalAttrs deployment.services.ingress {
      homelab-local-tls = {
        description = "Local canonical OIDC TLS ingress socket";
        wantedBy = [ "sockets.target" ];
        socketConfig.ListenStream = "127.0.0.1:443";
      };
    }
    // {
      podman.wantedBy = lib.mkForce [ ];
    };
  systemd.services =
    lib.mapAttrs' (
      name: ports:
      lib.nameValuePair "homelab-ingress-${name}" {
        description = "Forward LAN ${name} to rootless ingress";
        requires = [ "homelab-ingress-${name}.socket" ];
        after = [ "homelab-ingress-${name}.socket" ];
        serviceConfig = {
          ExecStart = "${pkgs.systemd}/lib/systemd/systemd-socket-proxyd 127.0.0.1:${toString ports.destination}";
          DynamicUser = true;
          NoNewPrivileges = true;
          PrivateTmp = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          RestrictAddressFamilies = [
            "AF_INET"
            "AF_UNIX"
          ];
          CapabilityBoundingSet = "";
          IPAddressDeny = "any";
          IPAddressAllow = [
            network.lanSubnet
            "localhost"
          ];
        };
      }
    ) forwards
    // lib.optionalAttrs deployment.services.ingress {
      homelab-local-tls = {
        description = "Forward local canonical OIDC TLS to rootless ingress";
        requires = [ "homelab-local-tls.socket" ];
        after = [ "homelab-local-tls.socket" ];
        serviceConfig = {
          ExecStart = "${pkgs.systemd}/lib/systemd/systemd-socket-proxyd 127.0.0.1:${toString servicePrincipals.ports.nginxHttps}";
          DynamicUser = true;
          NoNewPrivileges = true;
          PrivateTmp = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          RestrictAddressFamilies = [ "AF_INET" "AF_UNIX" ];
          CapabilityBoundingSet = "";
          IPAddressDeny = "any";
          IPAddressAllow = "localhost";
        };
      };
    }
    // {
      nixos-upgrade.preStart = ''
        ${approvalRuntime}/bin/homelab-approved-deployment update
      '';
    };

  # Root retains the approved code/profile snapshot. The timer can advance only
  # the stable vendor inputs, never a user checkout or newly rendered profile.
  system.autoUpgrade = {
    enable = true;
    flake = "path:/var/lib/nix-config/deployment#${systemConfig.system.hostname}";
    dates = "04:00";
    operation = "switch";
    allowReboot = true;
    rebootWindow = {
      lower = "04:00";
      upper = "06:00";
    };
    persistent = false;
    upgrade = false;
    flags = [ "--no-write-lock-file" ];
  };
}
