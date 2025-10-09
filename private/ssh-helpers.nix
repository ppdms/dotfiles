{ config, pkgs, ... }:

{
  # Zsh SSH helpers
  programs.zsh.initContent = ''
    # Set up SOPS environment
    export SOPS_AGE_KEY_FILE=~/.config/nix/private/age/keys.txt

    # Start SOPS SSH agent if not already running
    export SOPS_SSH_AUTH_SOCK="$HOME/.ssh/sops-agent.sock"
    if [[ ! -S "$SOPS_SSH_AUTH_SOCK" ]]; then
      # Start a new ssh-agent for SOPS keys
      ssh-agent -a "$SOPS_SSH_AUTH_SOCK" > /dev/null 2>&1 &
      sleep 0.1  # Give agent time to start
    fi

    # Command to ensure a key is loaded in SOPS agent
    # Used by SSH wrapper to auto-load keys
    sops-ensure-key() {
      local secret_name="$1"
      
      # Check if key is already in agent (ssh-add -l returns 0 if keys exist)
      if SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -l >/dev/null 2>&1; then
        return 0  # Key already loaded
      fi
      
      # Decrypt and add key
      local temp_dir=$(mktemp -d -t sops-ssh.XXXXXX)
      chmod 700 "$temp_dir"
      local temp_key="$temp_dir/key"
      
      if sops -d --extract "[\"$secret_name\"]" ~/.config/nix/private/secrets/secrets.yaml > "$temp_key" 2>/dev/null; then
        chmod 600 "$temp_key"
        SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -t 300 "$temp_key" 2>/dev/null
        local add_status=$?
        rm -P "$temp_key" 2>/dev/null || rm -f "$temp_key"
        rmdir "$temp_dir" 2>/dev/null
        return $add_status
      else
        rm -rf "$temp_dir"
        return 1
      fi
    }

    # Helper function to run SSH with key added just-in-time
    # Key is added to agent, SSH runs, then key is immediately removed
    ssh-with-agent() {
      local secret_name="$1"
      shift  # Remove first arg, rest are ssh command args
      
      local temp_dir=$(mktemp -d -t sops-ssh.XXXXXX)
      chmod 700 "$temp_dir"
      local temp_key="$temp_dir/key"
      
      # Decrypt key to temp file (will prompt for TouchID)
      if sops -d --extract "[\"$secret_name\"]" ~/.config/nix/private/secrets/secrets.yaml > "$temp_key" 2>/dev/null; then
        chmod 600 "$temp_key"
        
        # Add to SOPS agent temporarily
        SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add "$temp_key" 2>/dev/null
        local add_status=$?
        
        if [ $add_status -eq 0 ]; then
          # Get the key fingerprint for removal later
          local key_fp=$(SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-keygen -lf "$temp_key" 2>/dev/null | awk '{print $2}')
          
          # Run SSH with the SOPS agent
          SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh "$@"
          local ssh_status=$?
          
          # Remove key from agent immediately after SSH exits
          SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -d "$temp_key" 2>/dev/null
          
          # Securely remove the temporary files
          rm -P "$temp_key" 2>/dev/null || rm -f "$temp_key"
          rmdir "$temp_dir" 2>/dev/null
          
          return $ssh_status
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

    # Wrapper for transparent SSH with auto-decrypt and auto-cleanup
    # For specific hosts, automatically decrypts, loads, and removes keys
    ssh() {
      local host=""
      local use_sops=false
      
      # IMPORTANT! Check if any argument matches your SOPS patterns
      # Customize these patterns to match your specific hosts
      for arg in "$@"; do
        if [[ "$arg" =~ \.example\.com$ ]] || [[ "$arg" =~ ^your-pattern- ]]; then
          use_sops=true
          break
        fi
      done
      
      if $use_sops; then
        # Ensure key is loaded (replace with your secret name)
        sops-ensure-key your_ssh_key_name >/dev/null 2>&1
        
        # Run SSH with SOPS agent
        SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" command ssh "$@"
        local ssh_rc=$?
        
        # Remove key from agent after SSH exits
        SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -D >/dev/null 2>&1
        
        return $ssh_rc
      else
        # Use default SSH (Secretive agent or system default)
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

    # Start SOPS SSH agent if not already running
    set -gx SOPS_SSH_AUTH_SOCK "$HOME/.ssh/sops-agent.sock"
    if not test -S "$SOPS_SSH_AUTH_SOCK"
      # Start a new ssh-agent for SOPS keys
      ssh-agent -a "$SOPS_SSH_AUTH_SOCK" > /dev/null 2>&1 &
      sleep 0.1  # Give agent time to start
    end

    # Command to ensure a key is loaded in SOPS agent
    # Used by SSH wrapper to auto-load keys
    function sops-ensure-key
      set secret_name $argv[1]
      
      # Check if key is already in agent (ssh-add -l returns 0 if keys exist)
      if env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -l >/dev/null 2>&1
        return 0  # Key already loaded
      end
      
      # Decrypt and add key
      set temp_dir (mktemp -d -t sops-ssh.XXXXXX)
      chmod 700 "$temp_dir"
      set temp_key "$temp_dir/key"
      
      if sops -d --extract "[\"$secret_name\"]" $HOME/.config/nix/private/secrets/secrets.yaml > "$temp_key" 2>/dev/null
        chmod 600 "$temp_key"
        env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -t 300 "$temp_key" 2>/dev/null
        set add_status $status
        rm -P "$temp_key" 2>/dev/null; or rm -f "$temp_key"
        rmdir "$temp_dir" 2>/dev/null
        return $add_status
      else
        rm -rf "$temp_dir"
        return 1
      end
    end

    # Helper function to run SSH with key added just-in-time
    # Key is added to agent, SSH runs, then key is immediately removed
    function ssh-with-agent
      set secret_name $argv[1]
      set -e argv[1]  # Remove first arg, rest are ssh command args
      
      set temp_dir (mktemp -d -t sops-ssh.XXXXXX)
      chmod 700 "$temp_dir"
      set temp_key "$temp_dir/key"
      
      # Decrypt key to temp file (will prompt for TouchID)
      if sops -d --extract "[\"$secret_name\"]" $HOME/.config/nix/private/secrets/secrets.yaml > "$temp_key" 2>/dev/null
        chmod 600 "$temp_key"
        
        # Add to SOPS agent temporarily
        env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add "$temp_key" 2>/dev/null
        set add_status $status
        
        if test $add_status -eq 0
          # Get the key fingerprint for removal later
          set key_fp (env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-keygen -lf "$temp_key" 2>/dev/null | awk '{print $2}')
          
          # Run SSH with the SOPS agent
          env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh $argv
          set ssh_status $status
          
          # Remove key from agent immediately after SSH exits
          env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -d "$temp_key" 2>/dev/null
          
          # Securely remove the temporary files
          rm -P "$temp_key" 2>/dev/null; or rm -f "$temp_key"
          rmdir "$temp_dir" 2>/dev/null
          
          return $ssh_status
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

    # Wrapper for transparent SSH with auto-decrypt and auto-cleanup
    # For specific hosts, automatically decrypts, loads, and removes keys
    function ssh --wraps=ssh --description 'SSH wrapper with auto-decrypt for SOPS keys'
      set use_sops false
      
      # IMPORTANT! Check if any argument matches your SOPS patterns
      # Customize these patterns to match your specific hosts
      for arg in $argv
        if string match -qr '\.example\.com$' $arg; or string match -qr '^your-pattern-' $arg
          set use_sops true
          break
        end
      end
      
      if test "$use_sops" = true
        # Ensure key is loaded (replace with your secret name)
        sops-ensure-key your_ssh_key_name >/dev/null 2>&1
        
        # Run SSH with SOPS agent
        env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" command ssh $argv
        set ssh_rc $status
        
        # Remove key from agent after SSH exits
        env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -D >/dev/null 2>&1
        
        return $ssh_rc
      else
        # Use default SSH (Secretive agent or system default)
        command ssh $argv
      end
    end

    # Aliases and helper commands
    alias sops-clear='env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -D'  # Clear all keys from SOPS agent
    alias sops-list='env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -l'   # List keys in SOPS agent
  '';

  programs.ssh = {
    enable = true;
    enableDefaultConfig = false;
    matchBlocks = {
      # Hosts using SOPS-encrypted keys
      # (auto-decrypt handled by ssh wrapper function)
      # Customize this pattern to match your hosts
      "*.example.com" = {
        identityAgent = "~/.ssh/sops-agent.sock";
      };

      # All other hosts use Secretive (Secure Enclave keys) or system default
      "*" = {
        # Uncomment and adjust if using Secretive on macOS:
        # identityAgent = "/Users/YOUR-USERNAME/Library/Containers/com.maxgoedjen.Secretive.SecretAgent/Data/socket.ssh";
      };
    };
  };
}
