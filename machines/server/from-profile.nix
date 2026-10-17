# Build the server from the selected settings (the `profile` flake input).
# Takes the same argument and returns the same shape as
# machines/macbook/from-profile.nix, so both machines are built — and
# overridden with real, private settings — in exactly the same way.
# The server additionally hands back the tools it builds for itself.
{
  self,
  nixpkgs,
  home-manager,
  nixDarwinInput,
  sopsNixInput,
}:
profile:
let
  lib = nixpkgs.lib;
  systemConfig = import ../../settings/load-profile.nix {
    inherit lib profile;
    platform = "nixos";
  };
  system = systemConfig.system.platform;
  username = systemConfig.system.username;
  hostname = systemConfig.system.hostname;
  pkgs = nixpkgs.legacyPackages.${system};
  profileRoot = profile.outPath;
  servicePrincipals = import ../../services/service-accounts.nix { inherit lib systemConfig; };
  # Where the server's container-image repository lives on the machine.
  # It comes from the selected settings, never from this repository.
  imageFeed = profile.imageFeedPath;
  approvedDeployment = pkgs.callPackage ../../services/tools/approved-deployment.nix {
    boilerplate = self;
    inherit profile;
    nixpkgsInput = nixpkgs;
    homeManagerInput = home-manager;
    inherit nixDarwinInput sopsNixInput;
  };
  server = lib.nixosSystem {
    inherit system;
    specialArgs = {
      inherit
        systemConfig
        profileRoot
        imageFeed
        approvedDeployment
        servicePrincipals
        ;
      imagePins = profile.imagePins;
    };
    modules = [
      ./system.nix
      home-manager.nixosModules.home-manager
      {
        home-manager.useGlobalPkgs = true;
        # Use ~/.nix-profile for packages and Git/GH credential helper references.
        home-manager.useUserPackages = false;
        home-manager.backupFileExtension = "hm-backup";
        home-manager.extraSpecialArgs = { inherit profileRoot servicePrincipals; };
        home-manager.users.${username} = {
          imports = [
            ../shared/home.nix
            ./home.nix
          ];
        };
      }
    ];
  };
  instancePackages = self.packages.${system} // {
    approved-deployment = approvedDeployment;
    backrest-provision = server.config.system.build.homelabBackrestProvision;
    backrest-repository = server.config.system.build.homelabBackrestRepository;
    credential-broker = server.config.system.build.homelabCredentialBroker;
    service-state = server.config.system.build.homelabServiceState;
    creds = server.config.system.build.homelabCreds;
    tern = server.config.system.build.homelabTern;
    image-update = server.config.system.build.homelabImageUpdate;
    tree-dev = server.config.system.build.homelabTreeDev;
    image-build = server.config.system.build.homelabImageBuild;
    runtime-images = server.config.system.build.homelabImageSpec;
    quadlet-manifests = server.config.system.build.homelabQuadletManifests;
    service-principals = server.config.system.build.homelabPrincipals;
  };
in
assert lib.assertMsg (profile ? imagePins)
  "The selected NixOS profile is missing imagePins; render the approved private profile before building a production deployment.";
assert lib.assertMsg (
  lib.hasPrefix "/" imageFeed && !lib.hasPrefix "/nix/store/" imageFeed
) "imageFeed must name private writable runtime state outside /nix/store";
{
  nixosConfigurations.${hostname} = server;
  packages.${system} = instancePackages;
  apps.${system} = self.apps.${system} // {
    backrest-provision = {
      type = "app";
      program = "${instancePackages.backrest-provision}/bin/homelab-backrest-provision";
    };
    creds = {
      type = "app";
      program = "${instancePackages.creds}/bin/creds";
    };
    tern = {
      type = "app";
      program = "${instancePackages.tern}/bin/homelab-tern";
    };
    image-update = {
      type = "app";
      program = "${instancePackages.image-update}/bin/homelab-image-update";
    };
    backrest-repository = {
      type = "app";
      program = "${instancePackages.backrest-repository}/bin/homelab-backrest-repository";
    };
    credential-broker = {
      type = "app";
      program = "${instancePackages.credential-broker}/bin/homelab-credential-broker";
    };
    tree-dev = {
      type = "app";
      program = "${instancePackages.tree-dev}/bin/tree-dev";
    };
    image-build = {
      type = "app";
      program = "${instancePackages.image-build}/bin/homelab-image-build";
    };
  };
}
