{
  description = "macOS (Darwin) system configuration";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    nix-darwin = {
      url = "github:nix-darwin/nix-darwin";
      inputs.nixpkgs.follows = "nixpkgs";
    };
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
      nix-darwin,
      home-manager,
      sops-nix,
      profile,
    }:
    let
      systemConfig = profile.profiles.darwin;
    in
    {
      darwinConfigurations.${systemConfig.system.hostname} = nix-darwin.lib.darwinSystem {
        modules = [
          ./default.nix
          ../common/user.nix
          home-manager.darwinModules.home-manager
          sops-nix.darwinModules.sops
          {
            _module.args = {
              inherit
                systemConfig
                self
                ;
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
