{
  writeShellApplication,
  runCommand,
  callPackage,
  iproute2,
  secrets ? callPackage ./homelab-secrets.nix { },
}:

let
  cf = callPackage ./cf.nix { };
  sources = runCommand "homelab-vpn-ddns-source" { } ''
    mkdir -p "$out"
    cp ${./vpn-ddns.py} "$out/vpn-ddns.py"
    cp ${./vpn_dns.py} "$out/vpn_dns.py"
  '';
in
writeShellApplication {
  name = "homelab-vpn-ddns";
  runtimeInputs = [ iproute2 ];
  text = ''
    export PYTHONPATH=${secrets}/share/homelab
    export HOMELAB_CF=${cf}/bin/cf
    exec ${secrets.python}/bin/python3 -B ${sources}/vpn-ddns.py "$@"
  '';
}
