{
  description = "One Mac and one server, configured the same way";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    home-manager = {
      url = "github:nix-community/home-manager/release-26.05";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    nix-darwin = {
      url = "github:nix-darwin/nix-darwin/nix-darwin-26.05";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    sops-nix = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # The machine settings (hostname, user, disks, which services run, …).
    # This default is a placeholder that explains itself when forced; real
    # settings are private, live outside this repository, and are selected
    # with --override-input profile. See docs/settings-and-secrets.md.
    profile = {
      url = "path:./settings/placeholder-profile";
    };
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      home-manager,
      nix-darwin,
      sops-nix,
      profile,
      ...
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-darwin"
      ];
      forSystems = nixpkgs.lib.genAttrs systems;

      # Both machines are built by the same recipe: hand a folder in
      # machines/ the selected settings, get that machine's configuration.
      # Nothing else in this repository builds a machine.
      machines =
        selectedProfile:
        let
          server = import ./machines/server/from-profile.nix {
            inherit self nixpkgs home-manager;
            nixDarwinInput = nix-darwin;
            sopsNixInput = sops-nix;
          } selectedProfile;
        in
        {
          darwinConfigurations = import ./machines/macbook/from-profile.nix {
            inherit
              self
              nixpkgs
              nix-darwin
              home-manager
              sops-nix
              ;
          } selectedProfile;
          inherit (server) nixosConfigurations;
          # The server also builds the tools it runs (credential broker,
          # image updater, …); they belong to that machine, so they are
          # only exposed here, next to its configuration.
          inherit (server) packages apps;
        };
      selected = machines profile;
    in
    {
      inherit (selected) darwinConfigurations nixosConfigurations;

      # Used by the private deployment wrappers (tools/profile/) to rebuild
      # a machine from a frozen copy of this repository plus real settings,
      # instead of from a live checkout. Not needed for normal builds.
      lib.machines = machines;

      homeManagerModules = {
        shared = import ./machines/shared/home.nix;
        server = import ./services/default.nix;
      };
      nixosModules.server = import ./machines/server/system.nix;

      packages = forSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          profile = pkgs.callPackage ./tools/profile/package.nix { };
        }
        // nixpkgs.lib.optionalAttrs (system == "aarch64-darwin") {
          photos-import = pkgs.callPackage ./programs/photos-import/package.nix { };
        }
        // nixpkgs.lib.optionalAttrs (system == "x86_64-linux") {
          omp = pkgs.callPackage ./tools/omp.nix { };
          # Closed beta: allow this one unfree package, nothing else.
          tern =
            (import nixpkgs {
              inherit system;
              config.allowUnfreePredicate = package: nixpkgs.lib.getName package == "tern";
            }).callPackage
              ./programs/tern/package.nix
              { };
          omp-update = pkgs.callPackage ./programs/omp/update.nix { };
          cf = pkgs.callPackage ./services/tools/cf.nix { };
          home-manager = home-manager.packages.${system}.default;
        }
      );
      apps = forSystems (system: {
        profile = {
          type = "app";
          program = "${self.packages.${system}.profile}/bin/profile";
        };
      });
    };
}
