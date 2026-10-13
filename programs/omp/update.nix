{
  writeShellApplication,
  python3,
  nix,
}:

writeShellApplication {
  name = "homelab-omp-update";
  text = ''
    export HOMELAB_NIX=${nix}/bin/nix
    export HOMELAB_NIX_ENV=${nix}/bin/nix-env
    exec ${python3}/bin/python3 ${./update.py} "$@"
  '';
}
