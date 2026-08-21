{
  profileRoot,
  systemConfig,
  ...
}:

{
  sops = {
    age = {
      generateKey = false;
      keyFile = "${systemConfig.system.homeDirectory}/.config/sops/age/outer-key.txt";
    };

    defaultSopsFile = profileRoot + "/secrets.sops.yaml";

    secrets = {
      syncthing_key_pem = {
        owner = systemConfig.system.username;
        mode = "0600";
      };
      syncthing_cert_pem = {
        owner = systemConfig.system.username;
        mode = "0600";
      };
    };
  };
}
