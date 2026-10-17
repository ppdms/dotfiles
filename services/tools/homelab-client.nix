{
  lib,
  writeShellApplication,
  linkFarm,
  symlinkJoin,
  python3,
}:

let
  # Thin Quadlet-side client: layout + socket protocol + delivery only.
  # It must never gain a credential_validation dependency; its store path
  # is embedded in every Quadlet, so validation edits must not move it.
  pythonModules = linkFarm "homelab-credential-client-python" [
    {
      name = "credential_layout.py";
      path = ./credential_layout.py;
    }
    {
      name = "credential_client.py";
      path = ./credential_client.py;
    }
    {
      name = "homelab_client.py";
      path = ./homelab_client.py;
    }
  ];
  command = writeShellApplication {
    name = "homelab-secrets";
    text = ''
      export PYTHONPATH=${pythonModules}
      exec ${python3}/bin/python3 ${./homelab_client.py} "$@"
    '';
  };
in
symlinkJoin {
  name = "homelab-secrets-client";
  paths = [ command ];
  passthru = { python = python3; };
}
