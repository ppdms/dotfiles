{
  pkgs,
  config,
  lib,
  systemConfig,
  ...
}:
let
  # latexindent is installed via tlmgr (BasicTeX) but requires Perl modules not
  # present in macOS system Perl. This wraps it with a nix Perl that has the
  # required modules, installing the wrapper to the user's system profile.
  #
  # /Library/TeX/Distributions/Programs/texbin appears earlier in PATH, so VS Code
  # must be told to use the full path to this wrapper explicitly — see:
  # apps/vscode/settings.json → "latex-workshop.formatting.latexindent.path"
  latexindentPerl = pkgs.perl.withPackages (
    p: with p; [
      FileHomeDir
      YAMLTiny
      UnicodeLineBreak
    ]
  );
  latexindentWrapped = pkgs.writeShellScriptBin "latexindent" ''
    exec ${latexindentPerl}/bin/perl \
      /Library/TeX/Distributions/Programs/texbin/latexindent "$@"
  '';
  defaultSshKey = systemConfig.ssh.defaultKey;
  vscodeSettings = builtins.fromJSON (builtins.readFile ../../apps/vscode/settings.json) // {
    "geminicodeassist.project" = systemConfig.applications.vscode.geminiProject;
  };
  networkModeCases = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (
      name: mode:
      ''
        case ${lib.escapeShellArg name}
      ''
      + lib.optionalString (mode ? proxyPacUrl) ''
        set proxy_pac_url ${lib.escapeShellArg mode.proxyPacUrl}
      ''
      + lib.optionalString (mode ? dnsServers) ''
        set dns_servers ${lib.concatMapStringsSep " " lib.escapeShellArg mode.dnsServers}
      ''
    ) (systemConfig.network.modes or { })
  );
  claude = systemConfig.applications.claude;
  claudeConfig = {
    mcpServers.anki = {
      command = "npx";
      args = [
        "--yes"
        "anki-mcp-server"
      ];
    };
    coworkUserFilesPath = claude.coworkUserFilesPath;
    preferences = {
      launchPreviewPersistedWorkspaces = [ ];
      launchPreviewSessionScopedSessions = [ ];
      coworkScheduledTasksEnabled = true;
      coworkHipaaRestricted = false;
      ccdScheduledTasksEnabled = true;
      bypassPermissionsGateByAccount."${claude.accountId}" = false;
      coworkWebSearchEnabled = true;
      coworkModelAutoFallbackByAccount."${claude.accountId}" = true;
      remoteToolsDeviceName = claude.remoteToolsDeviceName;
      epitaxyPrefs = {
        "desktop-frame.paneStore.v1" = {
          state = {
            extraPanesByMode = { };
            colWeightsByMode = { };
            rowSplit = 0.5;
            draftNonce = 0;
          };
          version = 4;
        };
        starred-local-code-sessions = [ ];
        starred-cowork-spaces = [ ];
        starred-session-groups = [ ];
        dframe-group-scopes = { };
        dframe-local-slice = {
          pinnedOrder = [ ];
          homeProjectsPinnedOrder = [ ];
        };
        ccd-sessions-filter = {
          state.selectedProjects = [ ];
          version = 0;
        };
      };
      sidebarMode = "epitaxy";
      menuBarEnabled = false;
    };
  };
in
{
  home.packages = [ latexindentWrapped ];
  # Darwin-specific shell configuration
  programs.fish = {
    shellAliases = {
      # Darwin rebuild aliases (note: 'switch' is a reserved keyword in Fish)
      # Note: age-plugin-se automatically handles Secure Enclave keys, no SOPS_AGE_KEY_FILE needed
      rebuild = "bash $HOME/.config/nix/scripts/render-profile.sh; and sops exec-env $HOME/.local/state/nix-config/secrets.sops.yaml 'bash -c \"export NIX_CONFIG=\\\"access-tokens = github.com=\\$github_token\\\"; sudo --preserve-env=NIX_CONFIG darwin-rebuild switch --flake $HOME/.config/nix/systems/darwin --override-input profile path:$HOME/.local/state/nix-config\"'";
      rebuild-update = "bash $HOME/.config/nix/scripts/render-profile.sh; and cd $HOME/.config/nix/systems/darwin; and sops exec-env $HOME/.local/state/nix-config/secrets.sops.yaml 'bash -c \"export NIX_CONFIG=\\\"access-tokens = github.com=\\$github_token\\\"; sudo --preserve-env=NIX_CONFIG nix flake update --override-input profile path:$HOME/.local/state/nix-config; sudo --preserve-env=NIX_CONFIG darwin-rebuild switch --flake . --override-input profile path:$HOME/.local/state/nix-config\"'";
    };

    shellInit = ''
      # Initialize Homebrew
      eval (/opt/homebrew/bin/brew shellenv)

      # Set default SSH_AUTH_SOCK to SOPS agent
      set -gx SSH_AUTH_SOCK "$HOME/.ssh/sops-agent.sock"
    '';

    interactiveShellInit = ''
      # Network configuration function (DNS and Proxy)
      function net
        set mode $argv[1]
        set network_service ""
        set service_name ""
        set device_name ""

        # Get network services list and extract the first active one
        set lines (networksetup -listnetworkserviceorder)
        set i 1
        while test $i -le (count $lines)
          if string match -q "*Hardware Port:*" $lines[$i]
            if test $i -gt 1
              set idx (math $i - 1)
              set service_name (string replace -r '.*\)\s*' "" $lines[$idx])
              set device_name (string replace -r '.*Device: ([^)]*)\).*' '$1' $lines[$i])
              if test -n "$device_name"; and ipconfig getifaddr "$device_name" &>/dev/null
                set network_service "$service_name"
                break
              end
            end
          end
          set i (math $i + 1)
        end

        if test -z "$network_service"
          echo "No active network service found."
          return 1
        end

        echo "Using network service: $network_service"

        # Clear proxy and DNS by default
        set proxy_pac_url ""
        set dns_servers "Empty"

        switch $mode
          ${networkModeCases}
          case '*'
            # default/reset mode: no proxy, system default DNS
        end

        # Apply DNS settings
        networksetup -setdnsservers "$network_service" $dns_servers

        # Apply proxy PAC settings
        if test -n "$proxy_pac_url"
          networksetup -setautoproxyurl "$network_service" "$proxy_pac_url"
          networksetup -setautoproxystate "$network_service" on
        else
          networksetup -setautoproxystate "$network_service" off
        end
      end

      # VS Code shell integration
      if test "$TERM_PROGRAM" = "vscode"
        source "/Applications/Visual Studio Code.app/Contents/Resources/app/out/vs/workbench/contrib/terminal/common/scripts/shellIntegration.fish"
      end
    '';
  };

  home.file."Library/Application Support/Code/User/settings.json" = {
    force = true;
    text = builtins.toJSON vscodeSettings;
  };

  # Rclone configuration - decrypt and place directly in /var/root/.config/rclone/rclone.conf
  home.activation.rcloneConfig = config.lib.dag.entryAfter [ "writeBoundary" ] ''
    verboseEcho "=== Setting up rclone configuration ==="
    ${pkgs.fish}/bin/fish -c "
      set -x SOPS_AGE_KEY_FILE $HOME/.config/sops/age/outer-key.txt
      sudo mkdir -p /var/root/.config/rclone
      if ${pkgs.sops}/bin/sops -d --extract '[\"rclone_conf\"]' $HOME/.local/state/nix-config/secrets.sops.yaml | sudo tee /var/root/.config/rclone/rclone.conf > /dev/null
        sudo chmod 600 /var/root/.config/rclone/rclone.conf
        echo '✅ Rclone configuration updated'
      else
        echo '⚠️  Failed to decrypt rclone configuration'
      end
    "
  '';

  # # Docker configuration - set cliPluginsExtraDirs for Homebrew Docker plugins
  # home.activation.dockerConfig = config.lib.dag.entryAfter [ "writeBoundary" ] ''
  #   $DRY_RUN_CMD mkdir -p "$HOME/.docker"
  #   if [ -f "$HOME/.docker/config.json" ]; then
  #     # Backup existing config
  #     $DRY_RUN_CMD cp "$HOME/.docker/config.json" "$HOME/.docker/config.json.bak"
  #     # Merge with existing config using jq
  #     $DRY_RUN_CMD ${pkgs.jq}/bin/jq '. + {"cliPluginsExtraDirs": ["$HOMEBREW_PREFIX/lib/docker/cli-plugins"]}' "$HOME/.docker/config.json" > "$HOME/.docker/config.json.tmp"
  #     $DRY_RUN_CMD mv "$HOME/.docker/config.json.tmp" "$HOME/.docker/config.json"
  #   else
  #     # Create new config
  #     $DRY_RUN_CMD echo '{"cliPluginsExtraDirs": ["$HOMEBREW_PREFIX/lib/docker/cli-plugins"]}' > "$HOME/.docker/config.json"
  #   fi
  # '';

  # SOPS SSH Agent launchd service for Darwin
  launchd.agents.sops-ssh-agent = {
    enable = true;
    config = {
      ProgramArguments = [
        "${pkgs.openssh}/bin/ssh-agent"
        "-D"
        "-a"
        "${config.home.homeDirectory}/.ssh/sops-agent.sock"
      ];
      EnvironmentVariables = {
        SSH_ASKPASS = "/opt/homebrew/bin/ssh-askpass";
        DISPLAY = ":0";
      };
      KeepAlive = true;
      RunAtLoad = true;
      ProcessType = "Background";
    };
  };

  # Load the encrypted profile's default SSH key at startup.
  launchd.agents.sops-load-key = {
    enable = true;
    config = {
      ProgramArguments = [
        "${pkgs.writeShellScript "load-sops-key-darwin" ''
          # Set environment for SOPS and ssh-add.
          export HOME="${config.home.homeDirectory}"
          export SSH_AUTH_SOCK="${config.home.homeDirectory}/.ssh/sops-agent.sock"
          export SSH_ASKPASS="/opt/homebrew/bin/ssh-askpass"
          export DISPLAY=":0"
          export SOPS_AGE_KEY_FILE="${config.home.homeDirectory}/.config/sops/age/outer-key.txt"
          # Wait for the agent to respond, not merely for a stale socket.
          for i in {1..20}; do
            ${pkgs.openssh}/bin/ssh-add -l >/dev/null 2>&1
            agent_status=$?
            if [ "$agent_status" -eq 0 ] || [ "$agent_status" -eq 1 ]; then
              break
            fi
            sleep 0.5
          done

          # Decrypt and load the main key without per-use confirmation. The
          # The encrypted profile chooses which key should be loaded at login.
          TEMP_DIR=$(${pkgs.coreutils}/bin/mktemp -d -t sops-ssh.XXXXXX)
          ${pkgs.coreutils}/bin/chmod 700 "$TEMP_DIR"
          TEMP_KEY="$TEMP_DIR/key"

          if ${pkgs.sops}/bin/sops -d --extract '["${defaultSshKey}"]' "${config.home.homeDirectory}/.local/state/nix-config/secrets.sops.yaml" > "$TEMP_KEY" 2>/dev/null; then
            ${pkgs.coreutils}/bin/chmod 600 "$TEMP_KEY"
            # This dedicated agent must not retain a previously
            # confirm-constrained key across service generations.
            ${pkgs.openssh}/bin/ssh-add -D 2>/dev/null || true
            ${pkgs.openssh}/bin/ssh-add "$TEMP_KEY" 2>/dev/null
          fi
          ${pkgs.coreutils}/bin/rmdir "$TEMP_DIR" 2>/dev/null || true
        ''}"
      ];
      EnvironmentVariables = {
        # age-plugin-se must be on PATH or SOPS cannot decrypt secrets.yaml.
        PATH = "${pkgs.sops}/bin:${pkgs.age-plugin-se}/bin:${pkgs.openssh}/bin:${pkgs.coreutils}/bin:/usr/bin:/bin";
      };
      RunAtLoad = true;
      ProcessType = "Background";
    };
  };

  # Install required TeX Live packages via tlmgr after activation
  home.activation.texPackages = config.lib.dag.entryAfter [ "writeBoundary" ] ''
    if [ -x "/Library/TeX/Distributions/Programs/texbin/tlmgr" ]; then
      ${pkgs.fish}/bin/fish -c "
        eval (/opt/homebrew/bin/brew shellenv)
        /usr/bin/sudo /Library/TeX/Distributions/Programs/texbin/tlmgr update --self
        /usr/bin/sudo /Library/TeX/Distributions/Programs/texbin/tlmgr install \
          babel-greek cbfonts greek-fontenc greek-inputenc enumitem latexmk hyphen-greek latexindent
      " || true
    fi
  '';

  # Restart SOPS agent after LaunchAgents are installed.
  home.activation.restartSopsAgent = config.lib.dag.entryAfter [ "setupLaunchAgents" ] ''
    if [ -f "${config.home.homeDirectory}/.config/nix/scripts/restart-sops-agent.fish" ]; then
      $DRY_RUN_CMD ${pkgs.fish}/bin/fish "${config.home.homeDirectory}/.config/nix/scripts/restart-sops-agent.fish" || true
    fi
  '';

  # Build WireGuard gateway container with secret passed from environment
  # Kitty configuration - kitty is installed via Homebrew
  home.file.".config/kitty/kitty.conf".source = ../../apps/kitty/kitty.conf;
  home.file.".config/kitty/themes/Gruvbox_Dark_Hard.conf".source =
    ../../apps/kitty/Gruvbox_Dark_Hard.conf;

  # Ghostty configuration - ghostty is installed via Homebrew
  home.file.".config/ghostty/config".source = ../../apps/ghostty/config;

  # The Claude desktop app rewrites this file itself, so force = true prevents
  # clobber errors. Account and device identifiers come from the encrypted profile.
  home.file."Library/Application Support/Claude/claude_desktop_config.json".force = true;
  home.file."Library/Application Support/Claude/claude_desktop_config.json".text =
    builtins.toJSON claudeConfig;
}
