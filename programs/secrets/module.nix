{
  config,
  lib,
  profileRoot,
  pkgs,
  ...
}:
let
  rcloneConfigPath = "${config.home.homeDirectory}/.config/sops-nix/secrets/rclone_conf";
in

{
  home.sessionVariables = lib.optionalAttrs pkgs.stdenv.hostPlatform.isDarwin {
    RCLONE_CONFIG = rcloneConfigPath;
  };

  sops = {
    age = {
      generateKey = false;
      keyFile = "${config.home.homeDirectory}/.config/sops/age/outer-key.txt";
      plugins = lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.age-plugin-se ];
    };

    defaultSopsFile = profileRoot + "/secrets.sops.yaml";

    secrets = lib.optionalAttrs pkgs.stdenv.hostPlatform.isDarwin {
      rclone_conf = {
        # Keep rclone's native INI format in its own SOPS file instead of
        # embedding a multiline value in secrets.sops.yaml.
        sopsFile = profileRoot + "/rclone.ini";
        format = "ini";
        path = rcloneConfigPath;
      };
    };
  };
}
