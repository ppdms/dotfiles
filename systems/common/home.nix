{
  pkgs,
  config,
  systemConfig,
  ...
}:
let
  pythonWithPypdf = pkgs.python3.withPackages (ps: [ ps.pypdf ]);
in
{
  # this is internal compatibility configuration for home-manager,
  # don't change this!
  home.stateVersion = "25.05";
  # Let home-manager install and manage itself.
  programs.home-manager.enable = true;

  home.packages = with pkgs; [
    sops
    backrest
    restic
    rclone
    deno
    uv
    age
    ssh-to-age
    eslint
    openssh
    starship
    fzf
    grc
    fishPlugins.done
    fishPlugins.forgit
    fishPlugins.grc
    inter
    nerd-fonts.jetbrains-mono
    inconsolata
    anonymousPro
  ];

  # Import configuration (public SSH helpers + private secrets and SSH config)
  imports = [
    ../../modules/ssh-helpers.nix
    ../../apps/firefox/default.nix
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
    "/Library/TeX/Distributions/Programs/texbin"
    "$HOME/.config/nix/bin"
    "$HOME/.local/bin"
    "$HOME/Developer/go/bin"
  ];

  programs.fish = {
    enable = true;

    shellAliases = {
      profile-render = "bash $HOME/.config/nix/scripts/render-profile.sh";
      profile-seal = "bash $HOME/.config/nix/scripts/seal-profile.sh";
      profile = "profile-render; and $EDITOR $HOME/.local/state/nix-config/profile.json; and profile-seal";
      secrets = "bash $HOME/.config/nix/scripts/secrets-edit-backup.sh";

      # Utility aliases
      s = "kitten ssh";
      klar = "clear && printf '\\e[3J'";
    };

    shellInit = ''
      # Disable greeting message
      set -g fish_greeting

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
  };

  programs.git = {
    enable = true;

    settings = {
      user = {
        name = systemConfig.user.fullName;
        email = systemConfig.user.email;
      };

      alias = {
        now = "!f() { git add . && git commit -a -m \"$(date +%s)\"; }; f";
      };

      commit = {
        gpgsign = true;
      };

      init = {
        defaultBranch = "main";
      };

      push = {
        autoSetupRemote = true;
      };

      gpg = {
        format = "ssh";
      };

      "gpg \"ssh\"" = {
        allowedSignersFile = "~/.gitallowedsigners";
      };
    };

    ignores = [ ".DS_Store" ];

    signing = {
      key = systemConfig.user.gitSigningKey;
      signByDefault = true;
    };
  };

  # Create Git allowed signers file for SSH signature verification
  home.file.".gitallowedsigners".text = ''
    ${systemConfig.user.email} ${systemConfig.user.gitSigningKey}
  '';

  programs.neovim = {
    enable = true;
    viAlias = true;
    vimAlias = true;
    defaultEditor = true;
    withRuby = false;
    withPython3 = false;
    initLua = ''
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
        let g:tmpl_search_paths = ['${config.home.homeDirectory}/.config/nix/apps/vim-templates']
        let g:formatdef_latexindent = '"latexindent -"'
      endif

      " Key mappings
      vmap <C-C> "+y
      noremap <F3> :Autoformat<CR>
    '';
  };

  programs.starship = {
    enable = true;
    enableFishIntegration = true;
    settings = {
      "$schema" = "https://starship.rs/config-schema.json";
      format = "[](color_orange)$os$username[](bg:color_yellow fg:color_orange)$directory[](fg:color_yellow bg:color_aqua)$git_branch$git_status[](fg:color_aqua bg:color_blue)$c$cpp$rust$golang$nodejs$php$java$kotlin$haskell$python[](fg:color_blue bg:color_bg3)$docker_context$conda$pixi[](fg:color_bg3 bg:color_bg1)$time[ ](fg:color_bg1)$line_break$character";
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
        format = "[[ $symbol( $ version) ](fg:color_fg0 bg:color_blue)]($style)";
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

  home.file.".hushlogin".text = "";

  home.file.".config/nix/bin/python3".source = "${pythonWithPypdf}/bin/python3";
}
