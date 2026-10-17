# Build the Mac from the selected settings (the `profile` flake input).
# Takes the same argument and returns the same shape as
# machines/server/from-profile.nix, so both machines are built — and
# overridden with real, private settings — in exactly the same way.
{
  self,
  nixpkgs,
  nix-darwin,
  home-manager,
  sops-nix,
}:
profile:
let
  lib = nixpkgs.lib;
  exampleOnly = profile.exampleOnly or false;
  pkgs = nixpkgs.legacyPackages.${systemConfig.system.platform};
  refuseExampleActivation = pkgs.writeShellScript "refuse-example-mac-activation" ''
    echo "Activation refused: this is an example-only Mac profile without runtime credentials." >&2
    exit 1
  '';
  exampleGuard = "${refuseExampleActivation}";
  systemConfig = import ../../settings/load-profile.nix {
    inherit lib profile;
    platform = "darwin";
  };
  requiredCiphertext = [
    "secrets.sops.yaml"
    "rclone.ini"
  ];
in
# Force the selected settings first, so the placeholder input (used when
# nobody selected real settings) fails with its own helpful message.
builtins.deepSeq systemConfig (
  assert lib.assertMsg
    (exampleOnly || lib.all (name: builtins.pathExists (profile.outPath + "/${name}")) requiredCiphertext)
    "The selected Mac settings must contain secrets.sops.yaml and rclone.ini encrypted with SOPS; render the real settings before building.";
  {
    ${systemConfig.system.hostname} = nix-darwin.lib.darwinSystem {
      modules = [
        ./system.nix
        ../shared/user.nix
        home-manager.darwinModules.home-manager
        sops-nix.darwinModules.sops
        {
          system.activationScripts.preActivation.text =
            lib.mkIf exampleOnly (lib.mkBefore exampleGuard);

          _module.args = {
            inherit systemConfig self;
            profileRoot = profile.outPath;
          };

          home-manager.useGlobalPkgs = true;
          home-manager.useUserPackages = true;
          home-manager.verbose = true;
          home-manager.backupFileExtension = "backup";
          home-manager.users.${systemConfig.system.username} = { config, lib, ... }: {
            imports = [
              ../shared/home.nix
              ./home.nix
            ]
            ++ lib.optionals (!exampleOnly) [ ../../programs/secrets/module.nix ];
            home.activation.exampleOnly = lib.mkIf exampleOnly (
              config.lib.dag.entryBefore [ "writeBoundary" ] exampleGuard
            );
          };
          home-manager.sharedModules = lib.optionals (!exampleOnly) [
            sops-nix.homeManagerModules.sops
          ];
          home-manager.extraSpecialArgs = {
            inherit systemConfig;
            profileRoot = profile.outPath;
            flakeSelf = self;
          };
        }
      ];
    };
  }
)
