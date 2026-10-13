{
  config,
  pkgs,
  systemConfig,
  ...
}:
{
  imports = [
    ./hardware.nix
    ./runtime.nix
    ./accounts.nix
  ];
  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;
  boot.kernelPackages = pkgs.linuxPackages_latest;
  networking.hostName = systemConfig.system.hostname;
  networking.networkmanager.enable = true;
  time.timeZone = systemConfig.homelab.timeZone;
  i18n.defaultLocale = "en_US.UTF-8";
  i18n.extraLocaleSettings = builtins.listToAttrs (
    map
      (name: {
        inherit name;
        value = "el_GR.UTF-8";
      })
      [
        "LC_ADDRESS"
        "LC_IDENTIFICATION"
        "LC_MEASUREMENT"
        "LC_MONETARY"
        "LC_NAME"
        "LC_NUMERIC"
        "LC_PAPER"
        "LC_TELEPHONE"
        "LC_TIME"
      ]
  );

  services.tailscale.enable = true;
  services.xserver.xkb = {
    layout = "us";
    variant = "";
  };
  users.users.${systemConfig.system.username} = {
    isNormalUser = true;
    uid = systemConfig.system.uid;
    home = systemConfig.system.homeDirectory;
    description = systemConfig.user.fullName;
    shell = pkgs.fish;
    linger = true;
    extraGroups = [
      "networkmanager"
      "wheel"
    ];
    openssh.authorizedKeys.keys = systemConfig.user.authorizedKeys;
  };
  nixpkgs.config.allowUnfree = true;
  environment.systemPackages = [ pkgs.bubblewrap ];
  programs.fish.enable = true;
  programs.neovim = {
    enable = true;
    vimAlias = true;
  };
  services.openssh = {
    enable = true;
    openFirewall = true;
    authorizedKeysInHomedir = false;
    settings = {
      AllowUsers = [ systemConfig.system.username ];
      AuthenticationMethods = "publickey";
      PrintLastLog = false;
      PubkeyAuthentication = true;
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
      PermitEmptyPasswords = false;
    };
  };
  system.stateVersion = systemConfig.system.systemStateVersion;
}
