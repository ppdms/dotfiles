{
  description = "Placeholder for the locally rendered profile";

  outputs =
    { self }:
    let
      missing = throw ''
        The local profile input is missing. Restore it, then use:
          --override-input profile path:$HOME/.local/state/nix-config
      '';
    in
    {
      profiles.darwin = missing;
      profiles.nixos = missing;
      wireguard.nixos = missing;
    };
}
