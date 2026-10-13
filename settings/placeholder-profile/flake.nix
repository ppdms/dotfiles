{
  description = "Placeholder for private machine settings";

  outputs =
    { self }:
    let
      missing = throw ''
        No machine settings were selected. This repository never contains
        real settings; they live, encrypted, outside the checkout.
        Render them, ask where the rendered copy is, and select it:
          profile query --platform darwin --field input
        (use --platform nixos on the server, --state-dir for a nondefault
        state directory), then build with:
          --override-input profile path:PHYSICAL_GENERATION
        See docs/settings-and-secrets.md.
      '';
    in
    {
      profiles.darwin = missing;
      profiles.nixos = missing;
    };
}
