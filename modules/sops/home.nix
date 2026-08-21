{
  config,
  lib,
  profileRoot,
  pkgs,
  ...
}:

{
  sops = {
    age = {
      generateKey = false;
      keyFile = "${config.home.homeDirectory}/.config/sops/age/outer-key.txt";
      plugins = lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.age-plugin-se ];
    };

    defaultSopsFile = profileRoot + "/secrets.sops.yaml";

    secrets = lib.optionalAttrs pkgs.stdenv.hostPlatform.isDarwin {
      wireguard_wg0_conf = { };
      rclone_conf = { };
    };
  };
}
