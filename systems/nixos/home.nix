{
  pkgs,
  config,
  lib,
  systemConfig,
  ...
}:
let
  defaultSshKey = systemConfig.ssh.defaultKey;
  vscodeSettings =
    builtins.fromJSON (builtins.readFile ../../apps/vscode/settings.json)
    // lib.optionalAttrs (systemConfig ? applications.vscode.geminiProject) {
      "geminicodeassist.project" = systemConfig.applications.vscode.geminiProject;
    };
  networkModeCases = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (
      name: mode:
      ''
        case ${lib.escapeShellArg name}
      ''
      + lib.optionalString (mode ? proxyUrl) ''
        set -gx http_proxy ${lib.escapeShellArg mode.proxyUrl}
        set -gx https_proxy ${lib.escapeShellArg mode.proxyUrl}
      ''
      + lib.optionalString (mode ? noProxy) ''
        set -gx no_proxy ${lib.escapeShellArg (lib.concatStringsSep "," mode.noProxy)}
      ''
      + lib.optionalString (mode ? dnsServers) ''
        set dns_servers ${lib.concatMapStringsSep " " lib.escapeShellArg mode.dnsServers}
      ''
    ) (systemConfig.network.modes or { })
  );
in
{
  # NixOS-specific shell configuration
  programs.fish = {
    shellAliases = {
      # NixOS rebuild aliases
      rebuild = "bash $HOME/.config/nix/scripts/render-profile.sh; and env SOPS_AGE_KEY_FILE=$HOME/.config/sops/age/outer-key.txt sops -d --extract '[\"github_token\"]' $HOME/.local/state/nix-config/secrets.sops.yaml | tr -d '\\n' | read -l TOKEN; and env NIX_CONFIG=\"access-tokens = github.com=$TOKEN\" sudo -E nixos-rebuild switch --flake $HOME/.config/nix/systems/nixos --override-input profile path:$HOME/.local/state/nix-config";
      rebuild-update = "bash $HOME/.config/nix/scripts/render-profile.sh; and cd $HOME/.config/nix/systems/nixos; and env SOPS_AGE_KEY_FILE=$HOME/.config/sops/age/outer-key.txt sops -d --extract '[\"github_token\"]' $HOME/.local/state/nix-config/secrets.sops.yaml | tr -d '\\n' | read -l TOKEN; and env NIX_CONFIG=\"access-tokens = github.com=$TOKEN\" sudo -E nix flake update --override-input profile path:$HOME/.local/state/nix-config; and env NIX_CONFIG=\"access-tokens = github.com=$TOKEN\" sudo -E nixos-rebuild switch --flake . --override-input profile path:$HOME/.local/state/nix-config";
    };

    shellInit = ''
      # Set SSH_AUTH_SOCK to SOPS SSH agent
      set -gx SSH_AUTH_SOCK "$HOME/.ssh/sops-agent.sock"
      # Set SSH_ASKPASS for key confirmation dialogs
      set -gx SSH_ASKPASS "${pkgs.x11_ssh_askpass}/libexec/x11-ssh-askpass"
      set -gx DISPLAY ":0"
    '';

    interactiveShellInit = ''
      # Network configuration function (DNS and Proxy) for NixOS
      function net
        set mode $argv[1]

        # Clear proxy and DNS by default
        set -e http_proxy
        set -e https_proxy
        set -e no_proxy
        set dns_servers ""

        switch $mode
          ${networkModeCases}
          case '*'
            # Reset to system defaults
            set dns_servers ""
        end

        # Apply DNS settings using systemd-resolved
        if test -n "$dns_servers"
          echo "Setting DNS servers: $dns_servers"
          # Get the primary network interface
          set interface (ip route | grep default | awk '{print $5}' | head -n1)
          if test -n "$interface"
            sudo resolvectl dns $interface $dns_servers
            echo "DNS updated for interface: $interface"
          else
            echo "No active network interface found"
          end
        else if test "$mode" = "reset"
          # Reset DNS to default
          set interface (ip route | grep default | awk '{print $5}' | head -n1)
          if test -n "$interface"
            sudo resolvectl revert $interface
            echo "DNS reset to default for interface: $interface"
          end
        end
      end

      # VS Code shell integration
      if test "$TERM_PROGRAM" = "vscode"
        string replace --all -- "~" "$HOME" "$VSCODE_SHELL_INTEGRATION" | source
      end
    '';
  };

  home.file.".config/Code/User/settings.json" = {
    force = true;
    text = builtins.toJSON vscodeSettings;
  };

  # Kitty configuration - kitty is installed via Nix packages
  home.file.".config/kitty/kitty.conf".source = ../../apps/kitty/kitty.conf;
  home.file.".config/kitty/themes/Gruvbox_Dark_Hard.conf".source =
    ../../apps/kitty/Gruvbox_Dark_Hard.conf;

  # GNOME dconf settings to prevent sleep and screen blanking
  dconf.settings = {
    "org/gnome/desktop/screensaver" = {
      lock-enabled = false;
      idle-activation-enabled = false;
    };
    "org/gnome/desktop/session" = {
      idle-delay = 0; # Never idle (0 = disabled)
    };
    "org/gnome/settings-daemon/plugins/power" = {
      sleep-inactive-ac-type = "nothing";
      sleep-inactive-battery-type = "nothing";
      sleep-inactive-ac-timeout = 0;
      sleep-inactive-battery-timeout = 0;
      idle-dim = false;
    };
  };

  # Set SSH_AUTH_SOCK for all systemd user services and GUI applications
  systemd.user.sessionVariables = {
    SSH_AUTH_SOCK = "${config.home.homeDirectory}/.ssh/sops-agent.sock";
  };

  # Disable the default SSH agent service (we use SOPS SSH agent instead)
  services.ssh-agent.enable = false;

  # Disable GNOME Keyring's SSH component (gcr-ssh-agent)
  # We use our own SOPS SSH agent instead
  systemd.user.services.gcr-ssh-agent = {
    Unit = {
      RefuseManualStart = true;
      RefuseManualStop = true;
    };
    Install = {
      WantedBy = lib.mkForce [ ];
    };
  };

  # SOPS SSH Agent systemd service
  systemd.user.services.sops-ssh-agent = {
    Unit = {
      Description = "SOPS SSH Agent";
      After = [ "graphical-session-pre.target" ];
      PartOf = [ "graphical-session.target" ];
    };

    Service = {
      Type = "simple";
      Environment = [
        "SSH_ASKPASS=${pkgs.x11_ssh_askpass}/libexec/x11-ssh-askpass"
        "DISPLAY=:0"
      ];
      ExecStartPre = "${pkgs.coreutils}/bin/rm -f ${config.home.homeDirectory}/.ssh/sops-agent.sock";
      ExecStart = "${pkgs.openssh}/bin/ssh-agent -D -a ${config.home.homeDirectory}/.ssh/sops-agent.sock";
      Restart = "on-failure";
      RestartSec = "5s";
    };

    Install = {
      WantedBy = [ "graphical-session.target" ];
    };
  };

  # Load the encrypted profile's default SSH key at startup.
  systemd.user.services.sops-load-key = {
    Unit = {
      Description = "Load default key into SOPS SSH Agent";
      After = [ "sops-ssh-agent.service" ];
      Requires = [ "sops-ssh-agent.service" ];
    };

    Service = {
      Type = "oneshot";
      Environment = [
        "SSH_AUTH_SOCK=${config.home.homeDirectory}/.ssh/sops-agent.sock"
        "SSH_ASKPASS=${pkgs.x11_ssh_askpass}/libexec/x11-ssh-askpass"
        "DISPLAY=:0"
        "SOPS_AGE_KEY_FILE=${config.home.homeDirectory}/.config/sops/age/outer-key.txt"
      ];
      ExecStart = "${pkgs.writeShellScript "load-sops-key" ''
        # Wait for agent socket to be available
        for i in {1..10}; do
          if [ -S "${config.home.homeDirectory}/.ssh/sops-agent.sock" ]; then
            break
          fi
          sleep 0.5
        done

        # Decrypt and load the key with confirmation required (-c flag)
        TEMP_DIR=$(${pkgs.coreutils}/bin/mktemp -d -t sops-ssh.XXXXXX)
        ${pkgs.coreutils}/bin/chmod 700 "$TEMP_DIR"
        TEMP_KEY="$TEMP_DIR/key"

        if ${pkgs.sops}/bin/sops -d --extract '["${defaultSshKey}"]' "${config.home.homeDirectory}/.local/state/nix-config/secrets.sops.yaml" > "$TEMP_KEY"; then
          ${pkgs.coreutils}/bin/chmod 600 "$TEMP_KEY"
          # Use -c flag to require confirmation on every use of the key
          ${pkgs.openssh}/bin/ssh-add -c "$TEMP_KEY"
          ${pkgs.coreutils}/bin/rm -f "$TEMP_KEY"
        fi
        ${pkgs.coreutils}/bin/rmdir "$TEMP_DIR" 2>/dev/null || true
      ''}";
    };

    Install = {
      WantedBy = [ "graphical-session.target" ];
    };
  };

  # Run restart-sops-agent after home-manager activation
  home.activation.restartSopsAgent = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    if [ -f "${config.home.homeDirectory}/.config/nix/scripts/restart-sops-agent.fish" ]; then
      $DRY_RUN_CMD ${pkgs.fish}/bin/fish "${config.home.homeDirectory}/.config/nix/scripts/restart-sops-agent.fish" || true
    fi
  '';
}
