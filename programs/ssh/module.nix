{
  config,
  pkgs,
  lib,
  systemConfig,
  ...
}:

# This file contains generic SSH helper functions for SOPS key management.
# Host-specific patterns and settings come from the private system profile.

let
  # SSH configuration from systemConfig
  sshConfig = systemConfig.ssh;
  defaultKey = sshConfig.defaultKey or "";
  clipboardHosts = sshConfig.clipboardHosts or [ ];

  # Detect system type and set appropriate ssh-askpass path
  isDarwin = pkgs.stdenv.hostPlatform.isDarwin;
  sshAskpassPath =
    if isDarwin then
      "/opt/homebrew/bin/ssh-askpass"
    else
      "${pkgs.x11_ssh_askpass}/libexec/x11-ssh-askpass";

  # Merge all settings blocks from all keys
  allSettings = lib.foldl' (acc: key: acc // key.settings) { } sshConfig.keys;

  # Generate Fish host pattern checks for all keys
  fishPatternChecks = lib.concatMapStringsSep "\n        " (key: ''
    if ${
      lib.concatMapStringsSep "; or " (pattern: "string match -qr '${pattern}' $arg") key.hostPatterns
    }
      set use_sops true
      set sops_secret_name "${key.secretName}"
      set sops_key_ident "${if key ? keyFingerprint then key.keyFingerprint else key.keyComment}"
      set sops_require_confirm "${
        if key ? requireConfirm && !key.requireConfirm then "false" else "true"
      }"
      break
    end
  '') sshConfig.keys;

  # Fish switch statement resolving key identity and confirmation requirement
  # from configuration, used when sops-ensure-key / ssh-with-agent are called
  # without explicit arguments (so manual invocations respect requireConfirm).
  keyLookup = lib.concatMapStringsSep "\n      " (key: ''
    case ${lib.escapeShellArg key.secretName}
      set sops_key_ident "${if key ? keyFingerprint then key.keyFingerprint else key.keyComment}"
      set sops_require_confirm "${
        if key ? requireConfirm && !key.requireConfirm then "false" else "true"
      }"
  '') sshConfig.keys;
in
{
  # SSH configuration with settings blocks from the encrypted profile.
  programs.ssh = {
    enable = true;
    enableDefaultConfig = false;
    settings =
      allSettings
      // (
        # All other hosts use default agent (if configured)
        if sshConfig.defaultIdentityAgent != "" then
          {
            "*" = {
              IdentityAgent = sshConfig.defaultIdentityAgent;
            };
          }
        else
          { }
      );
  };

  # Fish SSH helpers
  programs.fish.functions = {
    # Command to ensure a key is loaded in SOPS agent
    # Usage: sops-ensure-key <secret_name> [key_ident] [require_confirm]
    # Defined as a function file so it auto-loads in non-interactive fish (e.g. ssh-vscode-sops)
    sops-ensure-key = ''
      set secret_name $argv[1]
      if test -z "$secret_name"
        set secret_name ${lib.escapeShellArg defaultKey}
      end
      if test -z "$secret_name"
        echo "No default SOPS SSH key is configured" >&2
        return 2
      end

      set key_ident ""
      set require_confirm "true"

      # Resolve identity and confirmation requirement from configuration;
      # explicit arguments (passed by the ssh wrapper) take precedence.
      switch "$secret_name"
        ${keyLookup}
      end

      if test (count $argv) -ge 2
        set key_ident $argv[2]
      end
      if test (count $argv) -ge 3
        set require_confirm $argv[3]
      end

      set sops_sock $HOME/.ssh/sops-agent.sock
      if test -n "$SOPS_SSH_AUTH_SOCK"
        set sops_sock "$SOPS_SSH_AUTH_SOCK"
      end

      # Check if key is already in agent
      if test -n "$key_ident"; and env SSH_AUTH_SOCK="$sops_sock" ssh-add -l 2>/dev/null | grep -F -q "$key_ident"
        return 0  # Key already loaded
      end

      # Decrypt and add key to agent
      set temp_key (mktemp -t sops-ssh.XXXXXX)
      chmod 600 "$temp_key"

      if sops -d --extract "[\"$secret_name\"]" $HOME/.local/state/nix-config/secrets.sops.yaml > "$temp_key" 2>/dev/null
        if test "$require_confirm" = "false"
          env SSH_AUTH_SOCK="$sops_sock" ssh-add "$temp_key" 2>/dev/null
        else
          # Add with -c flag to require confirmation on every use
          env SSH_AUTH_SOCK="$sops_sock" ssh-add -c "$temp_key" 2>/dev/null
        end
        set add_status $status
        rm -f "$temp_key"
        return $add_status
      else
        rm -f "$temp_key"
        echo "Failed to decrypt key from SOPS" >&2
        return 1
      end
    '';
  };

  programs.fish.interactiveShellInit = ''
    # Set up SOPS environment
    set -gx SOPS_AGE_KEY_FILE $HOME/.config/sops/age/outer-key.txt

    # Configure SSH_ASKPASS for confirmation prompts
    set -gx SSH_ASKPASS "${sshAskpassPath}"
    set -gx DISPLAY :0

    # Set SOPS SSH agent socket path
    set -gx SOPS_SSH_AUTH_SOCK "$HOME/.ssh/sops-agent.sock"

    # Helper function to run SSH with key loaded in SOPS agent
    # Usage: ssh-with-agent <secret_name> [ssh args...]
    function ssh-with-agent
      set secret_name $argv[1]
      set -e argv[1]  # Remove first arg, rest are ssh command args

      # Resolve confirmation requirement from configuration
      set sops_require_confirm "true"
      switch "$secret_name"
        ${keyLookup}
      end

      set temp_key (mktemp -t sops-ssh.XXXXXX)
      chmod 600 "$temp_key"

      # Decrypt and add key to agent
      if sops -d --extract "[\"$secret_name\"]" $HOME/.local/state/nix-config/secrets.sops.yaml > "$temp_key" 2>/dev/null
        if test "$sops_require_confirm" = "false"
          env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add "$temp_key" 2>/dev/null
        else
          env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -c "$temp_key" 2>/dev/null
        end
        set add_status $status
        rm -f "$temp_key"

        if test $add_status -eq 0
          # Run SSH with the SOPS agent
          env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh $argv
          return $status
        else
          echo "Failed to add key to agent" >&2
          return 1
        end
      else
        rm -f "$temp_key"
        echo "Failed to decrypt SSH key" >&2
        return 1
      end
    end

    # Upload a local clipboard image before connecting, then keep watching
    # for screenshots copied while the SSH session remains open.
    function __clipssh_before_connect
      set -g __clipssh_watch_pid ""
      if test (count $argv) -ne 1; or not command -q clipssh
        return 0
      end
      ${lib.optionalString (clipboardHosts != [ ]) ''
        switch "$argv[1]"
        case ${lib.concatMapStringsSep " " lib.escapeShellArg clipboardHosts}
          clipssh "$argv[1]" >/dev/null 2>/dev/null
          clipssh --watch "$argv[1]" >/dev/null 2>/dev/null &
          set -g __clipssh_watch_pid $last_pid
        end
      ''}
    end


    # Wrapper for transparent SSH with auto-decrypt
    # Configuration loaded from the encrypted profile (supports multiple keys).
    function ssh --wraps=ssh --description 'SSH wrapper for SOPS keys and clipboard sync'
      set -l use_sops false
      set -l sops_secret_name ""
      set -l sops_key_ident ""
      set -l sops_require_confirm ""

      # Check if any argument matches a configured SOPS pattern.
      for arg in $argv
        if not string match -q -- "-*" $arg
          ${fishPatternChecks}
        end
      end

      set -l clipssh_watch_pid
      if test "$use_sops" = true
        # Ensure the key is loaded (secret name and identity come from the match)
        if not sops-ensure-key "$sops_secret_name" "$sops_key_ident" "$sops_require_confirm"
          echo "Failed to load SSH key from SOPS" >&2
          return 1
        end

        # Run SSH with SOPS agent
        set -lx SSH_AUTH_SOCK "$SOPS_SSH_AUTH_SOCK"
        set -lx SSH_ASKPASS "$SSH_ASKPASS"
        if test "$sops_require_confirm" != "false"
          set -lx SSH_ASKPASS_REQUIRE "force"
        end
        set -lx DISPLAY "$DISPLAY"
        __clipssh_before_connect $argv
        set clipssh_watch_pid $__clipssh_watch_pid
        command ssh $argv
        set -l ssh_status $status
      else
        __clipssh_before_connect $argv
        set clipssh_watch_pid $__clipssh_watch_pid
        command ssh $argv
        set -l ssh_status $status
      end
      if test (count $clipssh_watch_pid) -gt 0
        kill $clipssh_watch_pid >/dev/null 2>&1
        wait $clipssh_watch_pid >/dev/null 2>&1
      end
      return $ssh_status
    end

    # Aliases and helper commands
    alias sops-clear='env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -D'  # Clear all keys from SOPS agent
    alias sops-list='env SSH_AUTH_SOCK="$SOPS_SSH_AUTH_SOCK" ssh-add -l'   # List keys in SOPS agent
  '';
}
