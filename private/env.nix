{ ... }:

{
  # System-specific configuration
  system = {
    hostname = "YOUR-HOSTNAME-HERE";
    username = "YOUR-USERNAME-HERE";
    homeDirectory = "/Users/YOUR-USERNAME-HERE";
  };

  # User information
  user = {
    fullName = "Your Full Name";
    email = "your.email@example.com";

    # Git signing key (if using Secretive or similar)
    gitSigningKey = "/path/to/your/signing/key.pub";
  };

  # SSH configuration for SOPS-encrypted keys
  # You can configure multiple SSH keys with different host patterns
  ssh = {
    # List of SOPS-encrypted SSH keys
    keys = [
      {
        # Name of the secret in secrets.yaml
        secretName = "your_ssh_key_secret_name";

        # Comment that identifies the key in ssh-add -l output
        keyComment = "your key comment";

        # Host patterns (regex) that should use this key
        # Patterns are checked with =~ in bash/zsh and string match -qr in fish
        hostPatterns = [
          "\\.example\\.com$" # Matches *.example.com
          "^myhost-" # Matches myhost-*
        ];

        # SSH matchBlocks for this key
        matchBlocks = {
          "*.example.com" = {
            identityAgent = "~/.ssh/sops-agent.sock";
          };
        };
      }

      # Example: Add another key for different hosts
      # {
      #   secretName = "another_ssh_key";
      #   keyComment = "Another SSH Key";
      #   hostPatterns = [
      #     "\\.corp\\.internal$"
      #   ];
      #   matchBlocks = {
      #     "*.corp.internal" = {
      #       identityAgent = "~/.ssh/sops-agent.sock";
      #     };
      #   };
      # }
    ];

    # Default SSH agent for all other hosts (Secretive/Secure Enclave keys)
    defaultIdentityAgent = "/Users/YOUR-USERNAME-HERE/Library/Containers/com.maxgoedjen.Secretive.SecretAgent/Data/socket.ssh";
  };
}
