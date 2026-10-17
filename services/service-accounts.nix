{ lib, systemConfig }:
let
  switches = systemConfig.homelab.services;
  make = name: slot: module: units: components: stateDirectories: {
    inherit
      name
      slot
      module
      units
      components
      stateDirectories
      ;
    account = "homelab-${name}";
    uid = 43000 + slot;
    gid = 43000 + slot;
    home = "/var/lib/homelab/users/${name}";
    stateRoot = "/var/lib/homelab/users/${name}/state";
    subUidStart = 1000000 + slot * 65536;
    subGidStart = 1000000 + slot * 65536;
    subIdCount = 65536;
    enabled = if module == null then switches.core else switches.${module};
  };
  definitions = {
    alloy = make "alloy" 0 "alloy" [ "alloy.service" ] [ "alloy" ] [ "alloy" ];
    authelia = make "authelia" 1 "ingress" [
      "authelia.service"
    ] [ "authelia" "authelia-oidc" ] [ "authelia" ];
    backrest = make "backrest" 2 "backrest" [ "backrest.service" ] [ "backrest" ] [ "backrest" ];
    cloudflared = make "cloudflared" 3 "ingress" [ "cloudflared.service" ] [ "cloudflared" ] [ ];
    esphome = make "esphome" 4 "homeassistant" [
      "esphome.service"
    ] [ "esphome" ] [ "homeassistant/esphome" ];
    homeassistant = make "homeassistant" 5 "homeassistant" [
      "homeassistant.service"
    ] [ "homeassistant" ] [ "homeassistant/config" ];
    image-updates = make "image-updates" 6 null [
      "homelab-image-observe.service"
      "homelab-tree-image-observe.service"
    ] [ "image-updates" ] [ ];
    immich = make "immich" 7 "immich" [
      "immich-database.service"
      "immich-redis.service"
      "immich-machine-learning.service"
      "immich-server.service"
      "immich-microservices.service"
    ] [ "immich" ] [ "immich" ];
    jellyfin = make "jellyfin" 8 "jellyfin" [ "jellyfin.service" ] [ ] [ "jellyfin" ];
    kavita = make "kavita" 9 "kavita" [ "kavita.service" ] [ "kavita" ] [ "kavita" ];
    nginx = make "nginx" 10 "ingress" [ "nginx.service" ] [ "nginx" ] [ ];
    opencloud = make "opencloud" 11 "opencloud" [ "opencloud.service" ] [ "opencloud" ] [ ];
    readeck = make "readeck" 12 "readeck" [ "readeck.service" ] [ "readeck" ] [ "readeck" ];
    silverbullet = make "silverbullet" 13 "silverbullet" [
      "silverbullet.service"
    ] [ ] [ "silverbullet" ];
    speedtest = make "speedtest" 14 "speedtest" [ "speedtest.service" ] [ ] [ ];
    termix = make "termix" 15 "termix" [ "termix.service" ] [ "termix" ] [ "termix" ];
    transmission = make "transmission" 16 "transmission" [
      "transmission-gluetun.service"
      "transmission.service"
    ] [ "transmission" ] [ "transmission" ];
    tree-eclass =
      (make "tree-eclass" 17 "tree-eclass" [
        "tree-eclass.service"
        "tree-eclass-migrate.service"
      ] [ "tree-eclass" ] [ "tree-eclass" ])
      // {
        # Finite release work is controlled explicitly, never enrolled in ordinary
        # workload startup, credential restarts or backup restoration.
        lifecycleUnits = [ "tree-eclass-deploy-migrate.service" ];
      };
    tree-dev = make "tree-dev" 18 "tree-eclass" [
      "tree-eclass-dev.service"
      "tree-eclass-dev-migrate.service"
    ] [ "tree-eclass" ] [ "tree-eclass-dev" ];
    vpn-ddns = make "vpn-ddns" 19 "vpn-ddns" [ "vpn-ddns.service" ] [ "vpn-ddns" ] [ ];
    metrics = make "metrics" 20 "metrics" [ "homelab-smart-metrics.service" ] [ ] [ "alloy-textfiles" ];
    credentials = make "credentials" 21 null [ "homelab-credentials.service" ] [ ] [ ];
    secrets-export = make "secrets-export" 22 null [
      "homelab-secrets-export.service"
    ] [ ] [ "secrets-export" ];
  };
  principals = lib.filterAttrs (_: role: role.enabled) definitions;
  ports = {
    nginxHttp = 18096;
    nginxHttps = 18443;
    authelia = 19091;
    backrest = 9898;
    homeassistant = 18097;
    esphome = 18098;
    immich = 18283;
    jellyfin = 18099;
    kavita = 18500;
    opencloud = 19200;
    readeck = 18800;
    silverbullet = 18300;
    speedtest = 18080;
    termix = 18081;
    transmission = 18095;
    tree-eclass = 18001;
    tree-dev = 18002;
    alloy = 12345;
    omp = 8377;
  };
in
{
  inherit principals definitions ports;
  origin = {
    directory = "/run/homelab-cloudflared-origin";
    group = "homelab-origin";
    gid = 43050;
  };
  socket = "/run/homelab-credentials/control.sock";
  credentialStore = "/var/lib/homelab-credentials";
  credentialSeedDirectory = "${systemConfig.system.homeDirectory}/.local/state/nix-config/service-credentials";
  hostLoopbackAddress = "169.254.1.3";
  metricsDirectory = "/var/lib/homelab-metrics";
  physicalStateRoot = systemConfig.homelab.stateRoot;
  operator = {
    inherit (systemConfig.system) username uid homeDirectory;
  };
  backup = {
    inherit (systemConfig.homelab.backup) ageRecipient;
    stateRoot = "${definitions.secrets-export.stateRoot}/secrets-export";
  };
}
