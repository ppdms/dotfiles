# ~/.config/nix/flake.nix

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
          system.defaults.finder.ShowPathbar = true;

          # Create /etc/zshrc that loads the nix-darwin environment.
          programs.zsh.enable = true;

          environment.systemPackages = with pkgs; [
            git
            openssh
            zsh
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

            taps = [ ];
            brews = [
              "neovim"
              "gh"
              "age-plugin-se"
            ];
            casks = [
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
          };

          home.sessionPath = [
            "$HOME/.local/bin"
          ];

          programs.zsh = {
            enable = true;
            autocd = true;
            enableCompletion = true;
            completionInit = "autoload -U colors && colors";

            shellAliases = {
              # Darwin rebuild aliases
              switch = ''SOPS_AGE_KEY_FILE=~/.config/nix/private/age/keys.txt sops -d --extract '["github_token"]' ~/.config/nix/private/secrets/secrets.yaml | tr -d '\n' | xargs -I {} sudo darwin-rebuild switch --flake ~/.config/nix/public --option access-tokens "github.com={}"'';
              switch-update = ''cd ~/.config/nix/public && SOPS_AGE_KEY_FILE=~/.config/nix/private/age/keys.txt sops -d --extract '["github_token"]' ~/.config/nix/private/secrets/secrets.yaml | tr -d '\n' | (read -r TOKEN; nix flake update --option access-tokens "github.com=$TOKEN" && sudo darwin-rebuild switch --flake . --option access-tokens "github.com=$TOKEN")'';
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
              # DNS switching function
              dns() {
                local network_service="Wi-Fi"
                case $1 in
                  pihole)
                    networksetup -setdnsservers $network_service 192.168.1.30
                    ;;
                  quad11)
                    networksetup -setdnsservers $network_service 9.9.9.11
                    ;;
                  *)
                    echo "Invalid argument. Usage: dns pihole or dns quad11"
                    return 1
                    ;;
                esac
                echo "DNS set to $1"
              }

              # GPG agent
              (gpg-connect-agent updatestartuptty /bye &>/dev/null &)

              # Starship prompt (if not in Apple Terminal)
              if [ "$TERM_PROGRAM" != "Apple_Terminal" ]; then 
                eval "$(starship init zsh)" 
              fi

              # Activate Python virtual environment if it exists
              if [ -f /Users/basil/Developer/env/bin/activate ]; then
                source /Users/basil/Developer/env/bin/activate
              fi
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
          };

          # VS Code settings management - create symlink from VS Code's location to our git-tracked file
          # This allows VS Code to write changes while keeping them in version control
          home.activation.vscodeSettings = config.lib.dag.entryAfter [ "writeBoundary" ] ''
            $DRY_RUN_CMD mkdir -p "$HOME/Library/Application Support/Code/User"
            $DRY_RUN_CMD rm -f "$HOME/Library/Application Support/Code/User/settings.json"
            $DRY_RUN_CMD ln -sf "$HOME/.config/nix/public/vscode-settings.json" "$HOME/Library/Application Support/Code/User/settings.json"
          '';

          # Kitty configuration - kitty is installed via Homebrew
          home.file.".config/kitty/kitty.conf".source = ./kitty.conf;

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
