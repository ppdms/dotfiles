{ ... }:
{
  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  # The system selects the login shell; Home Manager owns interactive tools.
  programs.fish.enable = true;
  nixpkgs.config.allowUnfree = true;
}
