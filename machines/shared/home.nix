{ config, systemConfig, ... }:
{
  imports = [
    ../../programs/shell/module.nix
    ../../programs/git/module.nix
    ../../programs/neovim/module.nix
    ../../programs/development/module.nix
    ../../programs/documents/module.nix
    ../../programs/omp/module.nix
  ];

  # The selected platform supplies its Home Manager compatibility version.
  home.stateVersion = systemConfig.system.homeStateVersion;
  home.username = systemConfig.system.username;
  home.homeDirectory = systemConfig.system.homeDirectory;
  programs.home-manager.enable = true;
  programs.bash.enable = true;

  home.sessionPath = [ "${config.home.homeDirectory}/.local/bin" ];
}
