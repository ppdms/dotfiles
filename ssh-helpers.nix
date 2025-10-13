{
  config,
  pkgs,
  lib,
  envConfig,
  ...
}:

# This file contains generic SSH helper functions for SOPS key management.
# Host-specific patterns and configurations are in private/env.nix

let
  # SSH configuration from envConfig
  sshConfig = envConfig.ssh;

  # Merge all matchBlocks from all keys
  allMatchBlocks = lib.foldl' (acc: key: acc // key.matchBlocks) { } sshConfig.keys;

  # Generate Zsh host pattern checks for all keys
  zshPatternChecks = lib.concatMapStringsSep "\n      " (key: ''
    if ${
      lib.concatMapStringsSep " || " (pattern: "[[ \"$arg\" =~ ${pattern} ]]") key.hostPatterns
    }; then
      use_sops=true
      sops_secret_name="${key.secretName}"
      sops_key_comment="${key.keyComment}"
      break
    fi
  '') sshConfig.keys;

  # Generate Fish host pattern checks for all keys
  fishPatternChecks = lib.concatMapStringsSep "\n        " (key: ''
    if ${
      lib.concatMapStringsSep "; or " (pattern: "string match -qr '${pattern}' $arg") key.hostPatterns
    }
      set use_sops true
      set sops_secret_name "${key.secretName}"
      set sops_key_comment "${key.keyComment}"
      break
    end
  '') sshConfig.keys;
in
{
  # SSH configuration with matchBlocks from private/env.nix
  programs.ssh = {
    enable = true;
    enableDefaultConfig = false;
    matchBlocks = allMatchBlocks // {
      # All other hosts use default agent (Secretive)
      "*" = {
        identityAgent = sshConfig.defaultIdentityAgent;
      };
    };
  };
  # Zsh SSH helpers
  programs.zsh.initContent = ''
    # Set up SOPS environment
    export SOPS_AGE_KEY_FILE=~/.config/nix/private/age/keys.txt

    # Configure SSH_ASKPASS for confirmation prompts (requires theseal/ssh-askpass)
    export SSH_ASKPASS="/opt/homebrew/bin/ssh-askpass"
    export SSH_ASKPASS_REQUIRE="force"
    export DISPLAY=:0

    # Start SOPS SSH agent if not already running
    export SOPS_SSH_AUTH_SOCK="$HOME/.ssh/sops-agent.sock"
    if [[ ! -S "$SOPS_SSH_AUTH_SOCK" ]]; then
      # Start a new ssh-agent for SOPS keys with SSH_ASKPASS environment
      SSH_ASKPASS="/opt/homebrew/bin/ssh-askpass" \
      DISPLAY=:0 \
      ssh-agent -a "$SOPS_SSH_AUTH_SOCK" > /dev/null 2>&1 &
      sleep 0.1  # Give agent time to start
    fi

    # Command to ensure a key is loaded in SOPS agent
    # Used by SSH wrapper to auto-load keys
    # Usage: sops-ensure-key <secret_name> <key_comment>
    sops-ensure-key() {
      local secret_name="$1"
      local key_comment="$2"
      
      # Check if key is already in agent (ssh-add -l returns 0 if keys exist, 1 if no keys)
      if SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -l >/dev/null 2>&1; then
        # Keys already exist, check if our specific key is there
        if SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -l | grep -q "$key_comment"; then
          return 0  # Our key is already loaded
        fi
      fi
      
      # Decrypt and add key
      local temp_dir=$(mktemp -d -t sops-ssh.XXXXXX)
      chmod 700 "$temp_dir"
      local temp_key="$temp_dir/key"
      
      if sops -d --extract "[\"$secret_name\"]" ~/.config/nix/private/secrets/secrets.yaml > "$temp_key"; then
        chmod 600 "$temp_key"
        # Use -c flag to require confirmation on every use of the key
        SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -c "$temp_key"
        local add_status=$?
        rm -P "$temp_key" 2>/dev/null || rm -f "$temp_key"
        rmdir "$temp_dir" 2>/dev/null
        return $add_status
      else
        rm -rf "$temp_dir"
        echo "Failed to decrypt key from SOPS" >&2
        return 1
      fi
    }

    # Helper function to run SSH with key added just-in-time
    # Key is added to agent, SSH runs, then key is immediately removed
    # Usage: ssh-with-agent <secret_name> [ssh args...]
    ssh-with-agent() {
      local secret_name="$1"
      shift  # Remove first arg, rest are ssh command args
      
      local temp_dir=$(mktemp -d -t sops-ssh.XXXXXX)
      chmod 700 "$temp_dir"
      local temp_key="$temp_dir/key"
      
      # Decrypt key to temp file (will prompt for TouchID)
      if sops -d --extract "[\"$secret_name\"]" ~/.config/nix/private/secrets/secrets.yaml > "$temp_key" 2>/dev/null; then
        chmod 600 "$temp_key"
        
        # Add to SOPS agent temporarily with confirmation required (-c flag)
        SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -c "$temp_key" 2>/dev/null
        local add_status=$?
        
        # Securely remove the temporary files immediately after adding to agent
        rm -P "$temp_key" 2>/dev/null || rm -f "$temp_key"
        rmdir "$temp_dir" 2>/dev/null
        
        if [ $add_status -eq 0 ]; then
          # Run SSH with the SOPS agent (will prompt for confirmation due to -c flag)
          SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh "$@"
          return $?
        else
          rm -P "$temp_key" 2>/dev/null || rm -f "$temp_key"
          rmdir "$temp_dir" 2>/dev/null
          echo "Failed to add key to agent" >&2
          return 1
        fi
      else
        rm -rf "$temp_dir"
        echo "Failed to decrypt SSH key" >&2
        return 1
      fi
    }

    # Wrapper for transparent SSH with auto-decrypt
    # Configuration loaded from private/env.nix (supports multiple keys)
    ssh() {
      local use_sops=false
      local sops_secret_name=""
      local sops_key_comment=""
      
      # Check if any argument matches our SOPS patterns (from env.nix)
      for arg in "$@"; do
        ${zshPatternChecks}
      done
      
      if $use_sops; then
        # Ensure key is loaded (secret name and key comment determined by pattern match)
        if ! sops-ensure-key "$sops_secret_name" "$sops_key_comment"; then
          echo "Failed to load SSH key from SOPS" >&2
          return 1
        fi
        
        # Run SSH with SOPS agent
        env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" \
            SSH_ASKPASS="$SSH_ASKPASS" \
            SSH_ASKPASS_REQUIRE="force" \
            DISPLAY="$DISPLAY" \
            command ssh "$@"
      else
        # Use default SSH (Secretive agent)
        command ssh "$@"
      fi
    }

    # Aliases and helper commands
    alias sops-clear='SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -D'  # Clear all keys from SOPS agent
    alias sops-list='SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -l'   # List keys in SOPS agent
  '';

  # Fish SSH helpers
  programs.fish.interactiveShellInit = ''
    # Set up SOPS environment
    set -gx SOPS_AGE_KEY_FILE $HOME/.config/nix/private/age/keys.txt

    # Configure SSH_ASKPASS for confirmation prompts (requires theseal/ssh-askpass)
    set -gx SSH_ASKPASS "/opt/homebrew/bin/ssh-askpass"
    set -gx SSH_ASKPASS_REQUIRE "force"
    set -gx DISPLAY :0

    # Start SOPS SSH agent if not already running
    set -gx SOPS_SSH_AUTH_SOCK "$HOME/.ssh/sops-agent.sock"
    if not test -S "$SOPS_SSH_AUTH_SOCK"
      # Start a new ssh-agent for SOPS keys with SSH_ASKPASS environment
      env SSH_ASKPASS="/opt/homebrew/bin/ssh-askpass" \
          DISPLAY=:0 \
          ssh-agent -a "$SOPS_SSH_AUTH_SOCK" > /dev/null 2>&1 &
      sleep 0.1  # Give agent time to start
    end

    # Command to ensure a key is loaded in SOPS agent
    # Used by SSH wrapper to auto-load keys
    # Usage: sops-ensure-key <secret_name> <key_comment>
    function sops-ensure-key
      set secret_name $argv[1]
      set key_comment $argv[2]
      
      # Check if key is already in agent (ssh-add -l returns 0 if keys exist, 1 if no keys)
      if env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -l >/dev/null 2>&1
        # Keys already exist, check if our specific key is there
        if env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -l | grep -q "$key_comment"
          return 0  # Our key is already loaded
        end
      end
      
      # Decrypt and add key
      set temp_dir (mktemp -d -t sops-ssh.XXXXXX)
      chmod 700 "$temp_dir"
      set temp_key "$temp_dir/key"
      
      if sops -d --extract "[\"$secret_name\"]" $HOME/.config/nix/private/secrets/secrets.yaml > "$temp_key"
        chmod 600 "$temp_key"
        # Use -c flag to require confirmation on every use of the key
        env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -c "$temp_key"
        set add_status $status
        rm -P "$temp_key" 2>/dev/null; or rm -f "$temp_key"
        rmdir "$temp_dir" 2>/dev/null
        return $add_status
      else
        rm -rf "$temp_dir"
        echo "Failed to decrypt key from SOPS" >&2
        return 1
      end
    end

    # Helper function to run SSH with key added just-in-time
    # Key is added to agent, SSH runs, then key is immediately removed
    # Usage: ssh-with-agent <secret_name> [ssh args...]
    function ssh-with-agent
      set secret_name $argv[1]
      set -e argv[1]  # Remove first arg, rest are ssh command args
      
      set temp_dir (mktemp -d -t sops-ssh.XXXXXX)
      chmod 700 "$temp_dir"
      set temp_key "$temp_dir/key"
      
      # Decrypt key to temp file (will prompt for TouchID)
      if sops -d --extract "[\"$secret_name\"]" $HOME/.config/nix/private/secrets/secrets.yaml > "$temp_key" 2>/dev/null
        chmod 600 "$temp_key"
        
        # Add to SOPS agent temporarily with confirmation required (-c flag)
        env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -c "$temp_key" 2>/dev/null
        set add_status $status
        
        # Securely remove the temporary files immediately after adding to agent
        rm -P "$temp_key" 2>/dev/null; or rm -f "$temp_key"
        rmdir "$temp_dir" 2>/dev/null
        
        if test $add_status -eq 0
          # Run SSH with the SOPS agent (will prompt for confirmation due to -c flag)
          env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh $argv
          return $status
        else
          rm -P "$temp_key" 2>/dev/null; or rm -f "$temp_key"
          rmdir "$temp_dir" 2>/dev/null
          echo "Failed to add key to agent" >&2
          return 1
        end
      else
        rm -rf "$temp_dir"
        echo "Failed to decrypt SSH key" >&2
        return 1
      end
    end

    # Wrapper for transparent SSH with auto-decrypt
    # Configuration loaded from private/env.nix (supports multiple keys)
    function ssh --wraps=ssh --description 'SSH wrapper with auto-decrypt for SOPS keys'
      set -l use_sops false
      set -l sops_secret_name ""
      set -l sops_key_comment ""
      
      # Check if any argument matches our SOPS patterns (from env.nix)
      for arg in $argv
        if not string match -q -- "-*" $arg
          ${fishPatternChecks}
        end
      end
      
      if test "$use_sops" = true
        # Ensure key is loaded (secret name and key comment determined by pattern match)
        if not sops-ensure-key "$sops_secret_name" "$sops_key_comment"
          echo "Failed to load SSH key from SOPS" >&2
          return 1
        end
        
        # Run SSH with SOPS agent (using exec for full interactivity)
        exec env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" \
                 SSH_ASKPASS="$SSH_ASKPASS" \
                 SSH_ASKPASS_REQUIRE="force" \
                 DISPLAY="$DISPLAY" \
                 ssh $argv
      else
        # Use default SSH (Secretive agent)
        exec ssh $argv
      end
    end

    # Aliases and helper commands
    alias sops-clear='env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -D'  # Clear all keys from SOPS agent
    alias sops-list='env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -l'   # List keys in SOPS agent
  '';
}
