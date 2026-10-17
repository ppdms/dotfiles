{
  lib,
  writeShellApplication,
  runCommand,
  podman,
  skopeo,
  git,
  systemd,
  coreutils,
  secrets,
  imageFeed,
  seed ? ../config/image-pins-seed.json,
}:

let
  sources = runCommand "homelab-image-update-source" { } ''
    mkdir -p "$out"
    cp ${./image-update.py} "$out/image-update.py"
    cp ${./image_age.py} "$out/image_age.py"
    cp ${./image_registry.py} "$out/image_registry.py"
    cp ${./image_publication.py} "$out/image_publication.py"
    cp ${./dockerfile_inputs.py} "$out/dockerfile_inputs.py"
    cp ${./principals.py} "$out/principals.py"
  '';
in
writeShellApplication {
  name = "homelab-image-update";
  runtimeInputs = [
    podman
    skopeo
    git
    systemd
  ];
  text = ''
    export HOMELAB_IMAGE_FEED=${lib.escapeShellArg imageFeed}
    export HOMELAB_IMAGE_POLICY=${../config/image-policy.json}
    export HOMELAB_IMAGE_SEED=${seed}
    export HOMELAB_PODMAN=${podman}/bin/podman
    export HOMELAB_SKOPEO=${skopeo}/bin/skopeo
    export HOMELAB_CAT=${coreutils}/bin/cat
    export HOMELAB_GIT=${git}/bin/git
    export HOMELAB_SYSTEMCTL=${systemd}/bin/systemctl
    export HOMELAB_ENV=${coreutils}/bin/env
    export HOMELAB_SUDO=/run/wrappers/bin/sudo
    export HOMELAB_IMAGE_UPDATE="$0"
    export PYTHONPATH=${secrets}/share/homelab
    exec ${secrets.python}/bin/python3 -B ${sources}/image-update.py "$@"
  '';
}
