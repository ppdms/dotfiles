{
  pkgs,
  config,
  lib,
  systemConfig,
  ...
}:
let
  photosImport = pkgs.callPackage ../../programs/photos-import/package.nix { };
  applicationsDirectory =
    if config.targets.darwin.copyApps.enable then
      config.targets.darwin.copyApps.directory
    else
      config.targets.darwin.linkApps.directory;
  photosImportApp = "${config.home.homeDirectory}/${applicationsDirectory}/photos-import.app";
  hammerspoonConfig = lib.replaceStrings
    [ "@photosImportApp@" "@pngpaste@" ]
    [ (builtins.toJSON photosImportApp) (builtins.toJSON "${pkgs.pngpaste}/bin/pngpaste") ]
    (builtins.readFile ../../programs/hammerspoon/init.lua);
  defaultSshKey = systemConfig.ssh.defaultKey;
  zedSettings = (pkgs.formats.json { }).generate "zed-settings.json" (
    builtins.fromJSON (builtins.readFile ../../programs/zed/settings.json)
    // {
      ssh_connections = systemConfig.applications.zed.sshConnections;
    }
  );
  # Install settings as an atomic, private writable copy instead of a symlink.
  installZedSettings = pkgs.writeShellScript "zed-install-settings" ''
    set -eu
    directory="$HOME/.config/zed"
    ${pkgs.coreutils}/bin/mkdir -p "$directory"
    temporary="$(${pkgs.coreutils}/bin/mktemp "$directory/.settings.json.XXXXXX")"
    trap '${pkgs.coreutils}/bin/rm -f "$temporary"' EXIT
    ${pkgs.coreutils}/bin/install -m 600 "$1" "$temporary"
    ${pkgs.coreutils}/bin/mv -fT "$temporary" "$directory/settings.json"
    trap - EXIT
  '';
  codexPython = pkgs.python3.withPackages (ps: [ ps.tomlkit ]);
  codexSandboxMode = systemConfig.applications.codex.sandboxMode;
  setCodexSandbox = pkgs.writeScript "codex-set-sandbox" ''
    #!${codexPython}/bin/python3
    ${builtins.readFile ../../programs/codex/set-sandbox.py}
  '';
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
  imports = [
    ../../programs/ssh/module.nix
    ../../programs/firefox/module.nix
  ];

  assertions = [
    {
      assertion = config.targets.darwin.copyApps.enable || config.targets.darwin.linkApps.enable;
      message = "The Photos importer requires Home Manager's Darwin application installation.";
    }
  ];

  home.packages = with pkgs; [
    photosImport
    pngpaste
    backrest
    deno
    sshpass
    x11_ssh_askpass
    inter
    nerd-fonts.jetbrains-mono
    inconsolata
    anonymousPro
  ];

  # The laptop keeps its externally installed OMP executable.
  programs.omp.managePackage = false;

  home.sessionVariables = {
    LANG = "en_US.UTF-8";
    LC_ALL = "en_US.UTF-8";
    LC_CTYPE = "en_US.UTF-8";
    LANG_ALL = "en_US.UTF-8";
    GPG_TTY = "$(tty)";
    GOPATH = "$HOME/Developer/go";
  };
  home.sessionPath = [
    "${systemConfig.system.checkout}/commands"
    "$HOME/.elan/bin"
    "$HOME/Developer/go/bin"
  ];
  home.file.".hushlogin".text = "";

  home.file.".hammerspoon/init.lua".text = hammerspoonConfig;
  targets.darwin.defaults."org.hammerspoon.Hammerspoon".MJConfigFile =
    "${config.home.homeDirectory}/.hammerspoon/init.lua";

  # Darwin-specific shell configuration
  programs.fish = {
    shellAliases = {
      # Darwin rebuild aliases (note: 'switch' is a reserved keyword in Fish)
      rebuild = "bash ${lib.escapeShellArg "${systemConfig.system.checkout}/tools/profile/rebuild.sh"}";
      rebuild-update = "bash ${lib.escapeShellArg "${systemConfig.system.checkout}/tools/profile/rebuild.sh"} --update";
    };

    shellInit = ''
      # Initialize Homebrew
      eval (/opt/homebrew/bin/brew shellenv)

      # Set default SSH_AUTH_SOCK to SOPS agent
      set -gx SSH_AUTH_SOCK "$HOME/.ssh/sops-agent.sock"

      # Keep rclone on the user-owned SOPS-managed config after rebuilds.
      set -gx RCLONE_CONFIG "$HOME/.config/sops-nix/secrets/rclone_conf"

      # OMP: permit sessions launched from the home directory.
      function omp
        command omp --allow-home $argv
      end
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

    '';
  };

  # Reset declared public preferences and selected private SSH metadata on
  # activation. Zed can edit this regular 0600 file until the next activation.
  home.activation.zedSettings = config.lib.dag.entryAfter [ "writeBoundary" ] ''
    run ${installZedSettings} ${zedSettings}
  '';

  # Codex and ChatGPT share a writable config. Only the top-level sandbox mode
  # is declared here when selected; null leaves the file entirely unmanaged.
  home.activation.codexSandbox = lib.mkIf (codexSandboxMode != null) (
    config.lib.dag.entryAfter [ "writeBoundary" ] ''
      run ${setCodexSandbox} ${lib.escapeShellArg codexSandboxMode}
    ''
  );

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

          # Keep decrypted key bytes in this shell and ssh-add's stdin, never
          # a temporary file. A failed decrypt leaves the existing agent alone.
          if decrypted_key=$(${pkgs.sops}/bin/sops -d --extract '["${defaultSshKey}"]' "${config.home.homeDirectory}/.local/state/nix-config/secrets.sops.yaml" 2>/dev/null); then
            # This dedicated agent must not retain a previously
            # confirm-constrained key across service generations.
            ${pkgs.openssh}/bin/ssh-add -D 2>/dev/null || true
            printf '%s\n' "$decrypted_key" | ${pkgs.openssh}/bin/ssh-add - 2>/dev/null
            status=$?
            unset decrypted_key
            exit "$status"
          fi
          exit 1
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

  # Restart SOPS agent after LaunchAgents are installed.
  home.activation.restartSopsAgent = config.lib.dag.entryAfter [ "setupLaunchAgents" ] ''
    if [ -f ${lib.escapeShellArg "${systemConfig.system.checkout}/tools/profile/restart-sops-agent.fish"} ]; then
      $DRY_RUN_CMD ${pkgs.fish}/bin/fish ${lib.escapeShellArg "${systemConfig.system.checkout}/tools/profile/restart-sops-agent.fish"} || true
    fi
  '';

  # Kitty configuration - kitty is installed via Homebrew
  home.file.".config/kitty/kitty.conf".source = ../../programs/kitty/kitty.conf;
  home.file.".config/kitty/themes/Gruvbox_Dark_Hard.conf".source =
    ../../programs/kitty/Gruvbox_Dark_Hard.conf;

  # Ghostty configuration - ghostty is installed via Homebrew
  home.file.".config/ghostty/config".source = ../../programs/ghostty/config;

  # The Claude desktop app rewrites this file itself, so force = true prevents
  # clobber errors. Account and device identifiers come from the encrypted profile.
  home.file."Library/Application Support/Claude/claude_desktop_config.json".force = true;
  home.file."Library/Application Support/Claude/claude_desktop_config.json".text =
    builtins.toJSON claudeConfig;
}
