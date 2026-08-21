{ pkgs, systemConfig, ... }:
{
  # Enable experimental features for flakes and nix-command
  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  environment.systemPackages = with pkgs; [
    git
    ncdu
    lsof
    openssh
    sshpass
    ripgrep
    vim
    wget
    curl
    htop
    neovim
    tmux
    fish
    bash
    coreutils
    findutils
    gawk
    gnugrep
    gnused
    rsync
    openssl
    cmake
    gnumake
    pkg-config
    tree
    python313
    python313Packages.pip
    nodejs_22
    rustup
    starship
    nixfmt
    hugo
    fd
    arp-scan
    nmap
    gh
    x11_ssh_askpass
  ];

  programs.fish.enable = true;

  nixpkgs.config.allowUnfree = true;
}
