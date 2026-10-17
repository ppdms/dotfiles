{
  python3,
  writeShellApplication,
  podman,
  git,
  systemd,
  coreutils,
  runCommand,
  imageUpdate,
  buildSpec,
}:

let
  sources = runCommand "homelab-image-build-source" { } ''
    mkdir -p "$out"
    cp ${./image-build.py} "$out/image-build.py"
    cp ${./image_build.py} "$out/image_build.py"
    cp ${./dockerfile_pins.py} "$out/dockerfile_pins.py"
    cp ${./dockerfile_inputs.py} "$out/dockerfile_inputs.py"
    cp ${./image_age.py} "$out/image_age.py"
    cp ${./image_registry.py} "$out/image_registry.py"
    cp ${./image_publication.py} "$out/image_publication.py"
    cp ${./principals.py} "$out/principals.py"
  '';
in
writeShellApplication {
  name = "homelab-image-build";
  runtimeInputs = [
    podman
    git
    systemd
    imageUpdate
  ];
  text = ''
    export HOMELAB_BUILD_SPEC=${buildSpec}
    export HOMELAB_PODMAN=${podman}/bin/podman
    export HOMELAB_CAT=${coreutils}/bin/cat
    export HOMELAB_GIT=${git}/bin/git
    export HOMELAB_SYSTEMCTL=${systemd}/bin/systemctl

    export HOMELAB_ENV=${coreutils}/bin/env
    export HOMELAB_SUDO=/run/wrappers/bin/sudo
    export HOMELAB_PYTHON=${python3}/bin/python3
    export HOMELAB_IMAGE_UPDATE=${imageUpdate}/bin/homelab-image-update
    export HOMELAB_SIGNATURE_POLICY=${../containers/policy.json}
    export HOMELAB_IMAGE_BUILD="$0"
    exec ${python3}/bin/python3 -B ${sources}/image-build.py "$@"
  '';
}
