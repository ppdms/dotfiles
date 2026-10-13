{
  lib,
  writeShellApplication,
  acl,
  secrets,
  stateRoot,
}:

writeShellApplication {
  name = "homelab-secrets-export";
  text = ''
    export HOMELAB_STATE_ROOT=${lib.escapeShellArg stateRoot}
    export HOMELAB_SETFACL=${acl}/bin/setfacl
    export PYTHONPATH=${lib.escapeShellArg "${secrets}/share/homelab"}
    exec ${secrets.python}/bin/python3 ${./secrets-export.py} "$@"
  '';
}
