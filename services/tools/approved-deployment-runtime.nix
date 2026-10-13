{
  writeShellApplication,
  python3,
  nix,
}:
writeShellApplication {
  name = "homelab-approved-deployment";
  text = ''
    export HOMELAB_NIX=${nix}/bin/nix
    exec ${python3}/bin/python3 ${./approved-deployment.py} "$@"
  '';
}
