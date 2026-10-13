{
  writeShellApplication,
  runCommand,
  python3,
  podman,
  coreutils,
}:

let
  sources = runCommand "homelab-quadlet-metrics-source" {} ''
    mkdir -p "$out"
    cp ${./quadlet-metrics.py} "$out/quadlet-metrics.py"
    cp ${./principals.py} "$out/principals.py"
  '';
in
writeShellApplication {
  name = "homelab-quadlet-metrics";
  runtimeInputs = [ python3 ];
  text = ''
    export HOMELAB_PODMAN=${podman}/bin/podman
    export HOMELAB_ENV=${coreutils}/bin/env
    exec ${python3}/bin/python3 -B ${sources}/quadlet-metrics.py "$@"
  '';
}
