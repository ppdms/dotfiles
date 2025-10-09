{ config, ... }:

{
  sops = {
    age.keyFile = "${config.home.homeDirectory}/.config/nix/private/age/keys.txt";
    defaultSopsFile = ./secrets.yaml;

    # Disable automatic decryption - we use on-demand decryption via shell functions
    age.generateKey = false;

    secrets = {
      # Secrets are decrypted on-demand, not automatically installed
      # Use ssh-with-agent helper function or the ssh wrapper to decrypt SSH keys temporarily
      # Example:
      # your_ssh_key_name = {
      #   sopsFile = ./secrets.yaml;
      # };
    };
  };
}
