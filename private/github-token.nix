{ config, ... }:

{
  # GitHub access token configuration
  # The token is stored in secrets.yaml and must be decrypted manually
  # Use the switch alias which handles decryption automatically

  sops.secrets.github_token = {
    sopsFile = ./secrets/secrets.yaml;
  };
}
