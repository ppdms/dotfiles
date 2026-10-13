{
  writeShellApplication,
  runCommand,
  python3,
}:

let
  sources = runCommand "homelab-smart-metrics-source" {} ''
    mkdir -p "$out"
    cp ${./smart-metrics.py} "$out/smart-metrics.py"
    cp ${./principals.py} "$out/principals.py"
  '';
in
writeShellApplication {
  name = "homelab-smart-metrics";
  runtimeInputs = [ python3 ];
  text = ''
    exec ${python3}/bin/python3 -B ${sources}/smart-metrics.py "$@"
  '';
}
