# Services that can run from an image built out of a local git checkout
# instead of a pinned registry image. See docs/self-built-images.md.
#
# Only static facts live here. The private settings choose which of these
# services use a checkout, and where it is:
#
#   homelab.selfBuilt.<name> = { checkout = "/abs/path"; serve = false; };
#
# `serve = false` builds and stages the image (`homelab-image-build deploy`)
# while the service keeps running its pinned registry image. Set `serve = true`
# once an image has been deployed to switch the service over.
#
# Tree is not listed: its release also checkpoints and migrates application
# data, so it keeps its own `tree-dev` command on top of the same build
# pipeline (docs/updating.md).
{ lib, systemConfig }:
let
  catalogue = {
    silverbullet = {
      principal = "silverbullet";
      unit = "silverbullet.service";
      container = "silverbullet";
      repository = "localhost/silverbullet";
      dockerfile = "Dockerfile.source";
      # The Dockerfile declares this build argument; the build tool passes it
      # the committed revision so the built version names its commit.
      revisionBuildArg = "SOURCE_REVISION";
    };
  };
  declared = systemConfig.homelab.selfBuilt;
  switches = systemConfig.homelab.services;
in
lib.mapAttrs (
  name: static:
  static
  // {
    inherit name;
    inherit (declared.${name}) checkout serve;
    image = "${static.repository}:stable";
  }
) (lib.filterAttrs (name: _: declared ? ${name} && switches.${name}) catalogue)
