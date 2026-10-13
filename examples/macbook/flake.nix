{
  description = "Made-up Mac settings for examples and checks; never a real machine";
  # Select explicitly with:
  # nix eval --raw .#darwinConfigurations.example-laptop.system.drvPath \
  #   --override-input profile path:./examples/macbook
  # This profile contains settings only. Home Manager secret declarations are
  # excluded; system and Home Manager activation reject this profile.
  outputs = { self }: {
    exampleOnly = true;
    profiles = (builtins.fromJSON (builtins.readFile ./profile.json)).profiles;
  };
}
