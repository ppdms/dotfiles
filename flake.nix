# ~/.config/nix/public/flake.nix

{
  description = "My system configuration";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    nix-darwin = {
      url = "github:nix-darwin/nix-darwin";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    sops-nix = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # Private configuration (includes secrets and ssh-helpers)
    nix-private = {
      url = "git+file:/Users/basil/.config/nix/private";
      flake = false;
    };
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      nix-darwin,
      home-manager,
      sops-nix,
      nix-private,
    }:
    let
      # Import system-specific configuration from private repo
      systemConfig = import "${nix-private}/system-config.nix" { };

      configuration =
        { pkgs, ... }:
        {

          nix.enable = false;
          # Necessary for using flakes on this system.
          nix.settings.experimental-features = "nix-command flakes";
          # GitHub access token is passed via --option flag in switch alias

          system.configurationRevision = self.rev or self.dirtyRev or null;

          # Used for backwards compatibility. please read the changelog
          # before changing: `darwin-rebuild changelog`.
          system.stateVersion = 4;

          # The platform the configuration will be used on.
          # If you're on an Intel system, replace with "x86_64-darwin"
          nixpkgs.hostPlatform = "aarch64-darwin";

          # Allow unfree packages
          nixpkgs.config.allowUnfree = true;

          # Declare the user that will be running `nix-darwin`.
          users.users.${systemConfig.system.username} = {
            name = systemConfig.system.username;
            home = systemConfig.system.homeDirectory;
          };

          system.primaryUser = systemConfig.system.username;

          security.pam.services.sudo_local.touchIdAuth = true;

          # Finder settings
          system.defaults.finder.ShowPathbar = false;
          system.defaults.finder.AppleShowAllExtensions = true;
          system.defaults.finder.FXDefaultSearchScope = "SCcf";
          system.defaults.finder.FXEnableExtensionChangeWarning = false;
          system.defaults.finder.NewWindowTarget = "Home";
          # system.defaults.finder.QuitMenuItem = true;
          system.defaults.finder.ShowExternalHardDrivesOnDesktop = false;
          system.defaults.finder.ShowRemovableMediaOnDesktop = false;
          system.defaults.finder._FXShowPosixPathInTitle = true;
          system.defaults.finder._FXSortFoldersFirst = true;
          system.defaults.finder._FXSortFoldersFirstOnDesktop = true;

          # Create /etc/zshrc and /etc/fish/config.fish that loads the nix-darwin environment.
          programs.zsh.enable = true;
          programs.fish.enable = true;

          environment.systemPackages = with pkgs; [
            git
            openssh
            sshpass
            zsh
            fish
            bash
            coreutils
            findutils
            gawk
            gnugrep
            gnused
            rsync
            curl
            openssl
            cmake
            gnumake
            pkg-config
            python313
            python313Packages.pip
            nodejs
            go
            rustup
            starship
            neovim
            tmux
            nixfmt-rfc-style
          ];

          homebrew = {
            enable = true;
            onActivation.cleanup = "zap";

            taps = [
              "theseal/ssh-askpass"
            ];
            brews = [
              "neovim"
              "gh"
              "age-plugin-se"
              "theseal/ssh-askpass/ssh-askpass"
            ];
            casks = [
              "volume-control"
              "losslessswitcher"
              "vlc"
              "royal-tsx"
              "obsidian"
              "dbeaver-community"
              "google-chrome"
              "maccy"
              "linearmouse"
              "winbox"
              "tailscale-app"
              "cloudflare-warp"
              "secretive"
              "keepassxc"
              "kekaexternalhelper"
              "keka"
              "betterdisplay"
              "font-jetbrains-mono-nerd-font"
              "rectangle"
              "visual-studio-code"
              "stats"
              "firefox"
              "iterm2"
              "microsoft-edge"
              "kitty"
            ];
          };
        };
      homeconfig =
        { pkgs, config, ... }:
        {
          # this is internal compatibility configuration for home-manager,
          # don't change this!
          home.stateVersion = "23.05";
          # Let home-manager install and manage itself.
          programs.home-manager.enable = true;

          home.packages = with pkgs; [
            sops
            age
            ssh-to-age
            oh-my-zsh
            openssh
            starship
            fzf
            grc
            fishPlugins.done
            fishPlugins.forgit
            fishPlugins.grc
          ];

          # Import private configuration (secrets and ssh-helpers)
          imports = [
            "${nix-private}/secrets/sops-config.nix"
            "${nix-private}/ssh-helpers.nix"
            (import ./firefox.nix {
              inherit pkgs config;
              inherit systemConfig;
            })
          ];

          home.sessionVariables = {
            # EDITOR is set by programs.neovim.defaultEditor = true
            LANG = "en_US.UTF-8";
            LC_ALL = "en_US.UTF-8";
            LC_CTYPE = "en_US.UTF-8";
            LANG_ALL = "en_US.UTF-8";
            GPG_TTY = "$(tty)";
            GOPATH = "$HOME/Developer/go";
          };

          home.sessionPath = [
            "$HOME/.local/bin"
            "$HOME/Developer/go/bin"
          ];

          programs.fish = {
            enable = true;

            shellAliases = {
              # Darwin rebuild aliases (note: 'switch' is a reserved keyword in Fish)
              rebuild = "env SOPS_AGE_KEY_FILE=$HOME/.config/nix/private/age/keys.txt sops -d --extract '[\"github_token\"]' $HOME/.config/nix/private/secrets/secrets.yaml | tr -d '\\n' | xargs -I {} sudo darwin-rebuild switch --flake $HOME/.config/nix/public --option access-tokens 'github.com={}'";
              rebuild-update = "cd $HOME/.config/nix/public; and env SOPS_AGE_KEY_FILE=$HOME/.config/nix/private/age/keys.txt sops -d --extract '[\"github_token\"]' $HOME/.config/nix/private/secrets/secrets.yaml | tr -d '\\n' | read -l TOKEN; and nix flake update --option access-tokens \"github.com=$TOKEN\"; and sudo darwin-rebuild switch --flake . --option access-tokens \"github.com=$TOKEN\"";
              secrets = "cd $HOME/.config/nix/private/secrets; and env SOPS_AGE_KEY_FILE=$HOME/.config/nix/private/age/keys.txt sops secrets.yaml";

              # Utility aliases
              s = "kitten ssh";
              klar = "clear && printf '\\e[3J'";
            };

            shellInit = ''
              # Disable greeting message
              set -g fish_greeting

              # Initialize Homebrew
              eval (/opt/homebrew/bin/brew shellenv)

              # Set default SSH_AUTH_SOCK to Secretive
              set -gx SSH_AUTH_SOCK "/Users/basil/Library/Containers/com.maxgoedjen.Secretive.SecretAgent/Data/socket.ssh"

              # Fish syntax highlighting colors
              set -g fish_color_autosuggestion '555' 'brblack'
              set -g fish_color_cancel -r
              set -g fish_color_command --bold
              set -g fish_color_comment red
              set -g fish_color_cwd green
              set -g fish_color_cwd_root red
              set -g fish_color_end brmagenta
              set -g fish_color_error brred
              set -g fish_color_escape 'bryellow' '--bold'
              set -g fish_color_history_current --bold
              set -g fish_color_host normal
              set -g fish_color_match --background=brblue
              set -g fish_color_normal normal
              set -g fish_color_operator bryellow
              set -g fish_color_param cyan
              set -g fish_color_quote yellow
              set -g fish_color_redirection brblue
              set -g fish_color_search_match 'bryellow' '--background=brblack'
              set -g fish_color_selection 'white' '--bold' '--background=brblack'
              set -g fish_color_user brgreen
              set -g fish_color_valid_path --underline
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
                  case work
                    set proxy_pac_url "http://proxyconf.glb.nokia.com/proxy.pac"
                  case home
                    set dns_servers "172.16.0.2" "fd00:dead:beef::2"
                  case default
                    set dns_servers "9.9.9.11" "2620:fe::fe"
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

            plugins = [
              {
                name = "done";
                src = pkgs.fishPlugins.done.src;
              }
              {
                name = "forgit";
                src = pkgs.fishPlugins.forgit.src;
              }
              {
                name = "grc";
                src = pkgs.fishPlugins.grc.src;
              }
            ];
          };

          # FZF configuration
          programs.fzf = {
            enable = true;
            enableFishIntegration = true;
            enableZshIntegration = true;
          };

          programs.zsh = {
            enable = true;
            autocd = true;
            enableCompletion = true;
            completionInit = "autoload -U colors && colors";

            shellAliases = {
              # Darwin rebuild aliases
              rebuild = ''SOPS_AGE_KEY_FILE=~/.config/nix/private/age/keys.txt sops -d --extract '["github_token"]' ~/.config/nix/private/secrets/secrets.yaml | tr -d '\n' | xargs -I {} sudo darwin-rebuild switch --flake ~/.config/nix/public --option access-tokens "github.com={}"'';
              rebuild-update = ''cd ~/.config/nix/public && SOPS_AGE_KEY_FILE=~/.config/nix/private/age/keys.txt sops -d --extract '["github_token"]' ~/.config/nix/private/secrets/secrets.yaml | tr -d '\n' | (read -r TOKEN; nix flake update --option access-tokens "github.com=$TOKEN" && sudo darwin-rebuild switch --flake . --option access-tokens "github.com=$TOKEN")'';
              secrets = "cd ~/.config/nix/private/secrets && SOPS_AGE_KEY_FILE=~/.config/nix/private/age/keys.txt sops secrets.yaml";

              # Utility aliases
              s = "kitten ssh";
              klar = "clear && printf '\\e[3J'";
            };

            sessionVariables = {
              SSH_AUTH_SOCK = "/Users/basil/Library/Containers/com.maxgoedjen.Secretive.SecretAgent/Data/socket.ssh";
              COMPLETION_WAITING_DOTS = "true";
              DISABLE_AUTO_TITLE = "true";
            };

            initContent = ''
              # Initialize Homebrew
              eval "$(/opt/homebrew/bin/brew shellenv)"

              # Network configuration function (DNS and Proxy)
              net() {
                local mode="$1"
                local network_service=""
                local service_name=""
                local device_name=""

                # Get network services list and extract the first active one
                local lines=("''${(@f)$(networksetup -listnetworkserviceorder)}")
                for ((i = 1; i < ''${#lines[@]}; i++)); do
                  if [[ "''${lines[i]}" =~ "Hardware Port:" ]]; then
                    service_name="''${lines[i - 1]##*) }"
                    device_name=$(echo "''${lines[i]}" | sed -n 's/.*Device: \([^)]*\))/\1/p')
                    if [[ -n "$device_name" ]] && ipconfig getifaddr "$device_name" &>/dev/null; then
                      network_service="$service_name"
                      break
                    fi
                  fi
                done

                if [[ -z "$network_service" ]]; then
                  echo "No active network service found."
                  return 1
                fi

                echo "Using network service: $network_service"

                # Clear proxy and DNS by default
                local proxy_pac_url=""
                local dns_servers=("Empty")

                case "$mode" in
                  work)
                    proxy_pac_url="http://proxyconf.glb.nokia.com/proxy.pac"
                    ;;
                  home)
                    dns_servers=("172.16.0.2" "fd00:dead:beef::2")
                    ;;
                  default)
                    dns_servers=("9.9.9.11" "2620:fe::fe")
                    ;;
                  *)
                    # default/reset mode: no proxy, system default DNS
                    ;;
                esac

                # Apply DNS settings
                networksetup -setdnsservers "$network_service" "''${dns_servers[@]}"

                # Apply proxy PAC settings
                if [[ -n "$proxy_pac_url" ]]; then
                  networksetup -setautoproxyurl "$network_service" "$proxy_pac_url"
                  networksetup -setautoproxystate "$network_service" on
                else
                  networksetup -setautoproxystate "$network_service" off
                fi
              }

              # # Initialize GPG agent
              # (gpg-connect-agent updatestartuptty /bye &>/dev/null &)

              [[ "$TERM_PROGRAM" == "vscode" ]] && . "/Applications/Visual Studio Code.app/Contents/Resources/app/out/vs/workbench/contrib/terminal/common/scripts/shellIntegration-rc.zsh"
            '';

            oh-my-zsh = {
              enable = true;
              plugins = [ ];
              theme = "jreese";
            };
          };

          programs.git = {
            enable = true;
            userName = systemConfig.system.username;
            userEmail = systemConfig.user.email;
            ignores = [ ".DS_Store" ];
            signing = {
              key = systemConfig.user.gitSigningKey;
              signByDefault = true;
            };
            extraConfig = {
              init.defaultBranch = "main";
              push.autoSetupRemote = true;
              gpg.format = "ssh";
              gpg.ssh.allowedSignersFile = "~/.gitallowedsigners";
            };
          };

          programs.neovim = {
            enable = true;
            viAlias = true;
            vimAlias = true;
            defaultEditor = true;
            extraLuaConfig = ''
              -- Basic settings
              vim.opt.number = true
              vim.opt.expandtab = true
              vim.opt.tabstop = 4
              vim.opt.shiftwidth = 4
              vim.opt.mouse = 'a'
              vim.opt.termguicolors = true
            '';
            extraConfig = ''
              " vim-plug setup
              let data_dir = stdpath('data') . '/site'
              if empty(glob(data_dir . '/autoload/plug.vim'))
                silent !curl -fLo ~/.local/share/nvim/site/autoload/plug.vim --create-dirs https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim
              endif

              " Only setup plugins if vim-plug is available
              if filereadable(expand(stdpath('data') . '/site/autoload/plug.vim'))
                " Plugin list
                call plug#begin(stdpath('data') . '/plugged')
                Plug 'honza/vim-snippets'
                Plug 'aperezdc/vim-template'
                Plug 'vim-autoformat/vim-autoformat'
                Plug 'nvim-treesitter/nvim-treesitter', {'do': ':TSUpdate'}
                call plug#end()
                
                " Plugin configuration
                let g:tmpl_author_name = '${systemConfig.user.fullName}'
                let g:tmpl_author_email = '${systemConfig.user.email}'
                let g:tmpl_search_paths = ['${config.home.homeDirectory}/.config/nix/public/vim-templates']
                let g:formatdef_latexindent = '"latexindent -"'
              endif

              " Key mappings
              vmap <C-C> "+y
              noremap <F3> :Autoformat<CR>
            '';
          };

          programs.starship = {
            enable = true;
            enableZshIntegration = true;
            enableFishIntegration = true;
            settings = {
              "$schema" = "https://starship.rs/config-schema.json";
              format = ''[](color_orange)$os$username[](bg:color_yellow fg:color_orange)$directory[](fg:color_yellow bg:color_aqua)$git_branch$git_status[](fg:color_aqua bg:color_blue)$c$cpp$rust$golang$nodejs$php$java$kotlin$haskell$python[](fg:color_blue bg:color_bg3)$docker_context$conda$pixi[](fg:color_bg3 bg:color_bg1)$time[ ](fg:color_bg1)$line_break$character'';
              palette = "gruvbox_dark";
              palettes.gruvbox_dark = {
                color_fg0 = "#fbf1c7";
                color_bg1 = "#3c3836";
                color_bg3 = "#665c54";
                color_blue = "#458588";
                color_aqua = "#689d6a";
                color_green = "#98971a";
                color_orange = "#d65d0e";
                color_purple = "#b16286";
                color_red = "#cc241d";
                color_yellow = "#d79921";
              };
              os = {
                disabled = false;
                style = "bg:color_orange fg:color_fg0";
                symbols = {
                  Windows = "󰍲";
                  Ubuntu = "󰕈";
                  SUSE = "";
                  Raspbian = "󰐿";
                  Mint = "󰣭";
                  Macos = "󰀵";
                  Manjaro = "";
                  Linux = "󰌽";
                  Gentoo = "󰣨";
                  Fedora = "󰣛";
                  Alpine = "";
                  Amazon = "";
                  Android = "";
                  Arch = "󰣇";
                  Artix = "󰣇";
                  EndeavourOS = "";
                  CentOS = "";
                  Debian = "󰣚";
                  Redhat = "󱄛";
                  RedHatEnterprise = "󱄛";
                  Pop = "";
                };
              };
              username = {
                show_always = true;
                style_user = "bg:color_orange fg:color_fg0";
                style_root = "bg:color_orange fg:color_fg0";
                format = "[ $user ]($style)";
              };
              directory = {
                style = "fg:color_fg0 bg:color_yellow";
                format = "[ $path ]($style)";
                truncation_length = 3;
                truncation_symbol = "…/";
                substitutions = {
                  "Documents" = "󰈙 ";
                  "Downloads" = " ";
                  "Music" = "󰝚 ";
                  "Pictures" = " ";
                  "Developer" = "󰲋 ";
                };
              };
              git_branch = {
                symbol = "";
                style = "bg:color_aqua";
                format = "[[ $symbol $branch ](fg:color_fg0 bg:color_aqua)]($style)";
              };
              git_status = {
                style = "bg:color_aqua";
                format = "[[($all_status$ahead_behind )](fg:color_fg0 bg:color_aqua)]($style)";
              };
              nodejs = {
                symbol = "";
                style = "bg:color_blue";
                format = "[[ $symbol( $version) ](fg:color_fg0 bg:color_blue)]($style)";
              };
              c = {
                symbol = " ";
                style = "bg:color_blue";
                format = "[[ $symbol( $version) ](fg:color_fg0 bg:color_blue)]($style)";
              };
              cpp = {
                symbol = " ";
                style = "bg:color_blue";
                format = "[[ $symbol( $version) ](fg:color_fg0 bg:color_blue)]($style)";
              };
              rust = {
                symbol = "";
                style = "bg:color_blue";
                format = "[[ $symbol( $version) ](fg:color_fg0 bg:color_blue)]($style)";
              };
              golang = {
                symbol = "";
                style = "bg:color_blue";
                format = "[[ $symbol( $version) ](fg:color_fg0 bg:color_blue)]($style)";
              };
              php = {
                symbol = "";
                style = "bg:color_blue";
                format = "[[ $symbol( $version) ](fg:color_fg0 bg:color_blue)]($style)";
              };
              java = {
                symbol = "";
                style = "bg:color_blue";
                format = "[[ $symbol( $version) ](fg:color_fg0 bg:color_blue)]($style)";
              };
              kotlin = {
                symbol = "";
                style = "bg:color_blue";
                format = "[[ $symbol( $version) ](fg:color_fg0 bg:color_blue)]($style)";
              };
              haskell = {
                symbol = "";
                style = "bg:color_blue";
                format = "[[ $symbol( $version) ](fg:color_fg0 bg:color_blue)]($style)";
              };
              python = {
                symbol = "";
                style = "bg:color_blue";
                format = "[[ $symbol( $version) ](fg:color_fg0 bg:color_blue)]($style)";
              };
              docker_context = {
                symbol = "";
                style = "bg:color_bg3";
                format = "[[ $symbol( $context) ](fg:#83a598 bg:color_bg3)]($style)";
              };
              conda = {
                style = "bg:color_bg3";
                format = "[[ $symbol( $environment) ](fg:#83a598 bg:color_bg3)]($style)";
              };
              pixi = {
                style = "bg:color_bg3";
                format = "[[ $symbol( $version)( $environment) ](fg:color_fg0 bg:color_bg3)]($style)";
              };
              time = {
                disabled = false;
                time_format = "%R";
                style = "bg:color_bg1";
                format = "[[  $time ](fg:color_fg0 bg:color_bg1)]($style)";
              };
              line_break = {
                disabled = false;
              };
              character = {
                disabled = false;
                success_symbol = "[](bold fg:color_green)";
                error_symbol = "[](bold fg:color_red)";
                vimcmd_symbol = "[](bold fg:color_green)";
                vimcmd_replace_one_symbol = "[](bold fg:color_purple)";
                vimcmd_replace_symbol = "[](bold fg:color_purple)";
                vimcmd_visual_symbol = "[](bold fg:color_yellow)";
              };
            };
          };

          # VS Code settings management - create symlink from VS Code's location to our git-tracked file
          # This allows VS Code to write changes while keeping them in version control
          home.activation.vscodeSettings = config.lib.dag.entryAfter [ "writeBoundary" ] ''
            $DRY_RUN_CMD mkdir -p "$HOME/Library/Application Support/Code/User"
            $DRY_RUN_CMD rm -f "$HOME/Library/Application Support/Code/User/settings.json"
            $DRY_RUN_CMD ln -sf "$HOME/.config/nix/public/vscode-settings.json" "$HOME/Library/Application Support/Code/User/settings.json"
          '';

          # Kitty configuration - kitty is installed via Homebrew
          home.file.".config/kitty/kitty.conf".source = ./kitty/kitty.conf;
          home.file.".config/kitty/themes/Gruvbox_Dark_Hard.conf".source = ./kitty/Gruvbox_Dark_Hard.conf;

          home.file.".hushlogin".text = "";

          # Vim templates directory
          home.file.".config/nix/public/vim-templates" = {
            source = ./vim-templates;
            recursive = true;
          };
        };
    in
    {
      darwinConfigurations.${systemConfig.system.hostname} = nix-darwin.lib.darwinSystem {
        modules = [
          configuration
          home-manager.darwinModules.home-manager
          sops-nix.darwinModules.sops
          {
            home-manager.useGlobalPkgs = true;
            home-manager.useUserPackages = true;
            home-manager.verbose = true;
            home-manager.users.${systemConfig.system.username} = homeconfig;
            home-manager.sharedModules = [
              sops-nix.homeManagerModules.sops
            ];
          }
        ];
      };
    };
}
