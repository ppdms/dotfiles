{
  lib,
  python3,
  writeShellApplication,
  podman,
  git,
  systemd,
  coreutils,
  runCommand,
  imageUpdate,
  treeCheckout,
}:

let
  sources = runCommand "tree-dev-source" { } ''
    mkdir -p "$out"
    cp ${./tree-dev.py} "$out/tree-dev.py"
    cp ${./principals.py} "$out/principals.py"
    cp ${./tree_recovery.py} "$out/tree_recovery.py"
  '';
in
writeShellApplication {
  name = "tree-dev";
  runtimeInputs = [
    podman
    git
    systemd
    imageUpdate
  ];
  text = ''
    export HOMELAB_TREE_CHECKOUT=${lib.escapeShellArg treeCheckout}
    export HOMELAB_TREE_RECOVERY=/var/lib/homelab/tree-releases
    export HOMELAB_PODMAN=${podman}/bin/podman
    export HOMELAB_CAT=${coreutils}/bin/cat
    export HOMELAB_GIT=${git}/bin/git
    export HOMELAB_SYSTEMCTL=${systemd}/bin/systemctl

    export HOMELAB_ENV=${coreutils}/bin/env
    export HOMELAB_SUDO=/run/wrappers/bin/sudo
    export HOMELAB_PYTHON=${python3}/bin/python3
    export HOMELAB_IMAGE_UPDATE=${imageUpdate}/bin/homelab-image-update
    export HOMELAB_SIGNATURE_POLICY=${../containers/policy.json}
    export HOMELAB_TREE_DEV="$0"
    exec ${python3}/bin/python3 -B ${sources}/tree-dev.py "$@"
  '';
}
