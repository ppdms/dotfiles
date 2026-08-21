{
  description = "NixOS system configuration";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    sops-nix = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    profile = {
      # Commands override this placeholder with the locally rendered profile.
      url = "path:../../modules/profile-placeholder";
    };
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      home-manager,
      sops-nix,
      profile,
    }:
    let
      systemConfig = profile.profiles.nixos;
      wireguardConfig = profile.wireguard.nixos;
    in
    {
      nixosConfigurations.${systemConfig.system.hostname} = nixpkgs.lib.nixosSystem {
        system = "aarch64-linux";
        modules = [
          ./configuration.nix
          home-manager.nixosModules.home-manager
          sops-nix.nixosModules.sops
          ../../modules/sops/nixos-system.nix
          {
            _module.args = {
              inherit systemConfig wireguardConfig;
              profileRoot = profile.outPath;
            };

            home-manager.useGlobalPkgs = true;
            home-manager.useUserPackages = true;
            home-manager.verbose = true;
            home-manager.backupFileExtension = "backup";
            home-manager.users.${systemConfig.system.username} = {
              imports = [
                ../common/home.nix
                ./home.nix
                ../../modules/sops/home.nix
              ];
            };
            home-manager.sharedModules = [
              sops-nix.homeManagerModules.sops
            ];
            home-manager.extraSpecialArgs = {
              inherit systemConfig;
              profileRoot = profile.outPath;
            };
          }
        ];
      };
    };
}
