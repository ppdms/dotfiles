{
  config,
  pkgs,
  systemConfig,
  ...
}:

{
  imports = [
    # Include the results of the hardware scan
    ./hardware-configuration.nix
    ./default.nix
    ../common/user.nix
  ];

  # User configuration
  users.users.${systemConfig.system.username} = {
    isNormalUser = true;
    description = systemConfig.user.fullName;
    shell = pkgs.fish;
    extraGroups = [
      "wheel"
      "networkmanager"
    ];
  };

  # Enable automatic GNOME Keyring unlock on login
  security.pam.services.gdm.enableGnomeKeyring = true;
  security.pam.services.gdm-password.enableGnomeKeyring = true;
  security.pam.services.login.enableGnomeKeyring = true;
  security.pam.services.passwd.enableGnomeKeyring = true;

  # Allow unfree packages (needed for vscode, google-chrome, obsidian, etc.)
  nixpkgs.config.allowUnfree = true;

  # Additional system packages beyond what's in systems/nixos/default.nix
  environment.systemPackages = with pkgs; [
    vim
    wget
  ];
}
