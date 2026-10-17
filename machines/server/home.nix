{
  lib,
  pkgs,
  systemConfig,
  ...
}:
let
  secrets = pkgs.callPackage ../../services/tools/homelab-secrets.nix {
    lanDomain = systemConfig.homelab.lanDomain;
    publicDomain = systemConfig.homelab.publicDomain;
  };
in
{
  imports = [
    ../../services/default.nix
    ../../programs/tern/module.nix
  ];
  homelab.keyring.enable = true;
  # Opt-in per host (profile homelab.services.tern): the pinned tarball sits
  # behind a sign-in, so the public example must not need it.
  programs.tern = {
    enable = systemConfig.homelab.services.tern or false;
    web = {
      enable = true;
      credentials = pkgs.callPackage ../../programs/tern/tool.nix {
        inherit secrets;
        checkout = systemConfig.system.checkout;
      };
    };
  };
  home.packages = [
    pkgs.cloudflared
    (pkgs.callPackage ../../services/tools/cf.nix { })
    (pkgs.callPackage ../../tools/profile/package.nix { })
  ];
  programs.fish.shellAliases = {
    rebuild = "bash ${lib.escapeShellArg systemConfig.system.checkout}/tools/profile/rebuild.sh";
    rebuild-update = "bash ${lib.escapeShellArg systemConfig.system.checkout}/tools/profile/rebuild.sh --update";
    clean = "bash ${lib.escapeShellArg systemConfig.system.checkout}/tools/clean.sh";
    cleanup = "bash ${lib.escapeShellArg systemConfig.system.checkout}/tools/clean.sh";
  };
}
