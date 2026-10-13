{
  description = "Made-up server settings for examples and checks; never a real machine";
  # Select explicitly with:
  # nix build .#nixosConfigurations.example-server.config.system.build.toplevel \
  #   --override-input profile path:./examples/server
  # Paths and domains are demonstration runtime locators, not provisioned state.
  outputs = { self }: {
    profiles = (builtins.fromJSON (builtins.readFile ./profile.json)).profiles;
    imagePins = (builtins.fromJSON (builtins.readFile ./image-pins.json)).images;
    imageFeedPath = "/home/example/.local/state/homelab/service-images";
  };
}
