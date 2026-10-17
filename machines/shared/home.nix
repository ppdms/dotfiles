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

  # Don't generate the `home-configuration.nix` man page: it is built
  # from the full options JSON, whose store references are discarded
  # upstream (nixosOptionsDoc), which makes current Nix warn on every
  # build that the resulting options.json derivation is unreliable.
  # We don't read that man page; the JSON/HTML manuals are already off.
  manual.manpages.enable = false;

  # Fish defaults this on (for `apropos` completion), which makes every
  # build run `mandb --create` locally over all installed man pages
  # (~7,500, mostly OpenSSL). Man pages still install; only the
  # prebuilt apropos/man -k index is skipped.
  programs.man.generateCaches = false;

  home.sessionPath = [ "${config.home.homeDirectory}/.local/bin" ];
}
