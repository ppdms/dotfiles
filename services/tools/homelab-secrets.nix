{
  lib,
  writeShellApplication,
  linkFarm,
  symlinkJoin,
  python3,
  openssl,
  lanDomain,
  publicDomain,
}:

let
  python = python3.withPackages (ps: [ ps.pyyaml ]);
  pythonModules = linkFarm "homelab-credential-python" [
    {
      name = "homelab_secrets.py";
      path = ./homelab-secrets.py;
    }
    {
      name = "credential_layout.py";
      path = ./credential_layout.py;
    }
    {
      name = "homelab_client.py";
      path = ./homelab_client.py;
    }
    {
      name = "credential_validation.py";
      path = ./credential_validation.py;
    }
    {
      name = "credential_client.py";
      path = ./credential_client.py;
    }
    {
      name = "credential_broker.py";
      path = ./credential_broker.py;
    }
    {
      name = "principals.py";
      path = ./principals.py;
    }
  ];
  command = writeShellApplication {
    name = "homelab-secrets";
    text = ''
      export HOMELAB_LAN_DOMAIN=${lib.escapeShellArg lanDomain}
      export HOMELAB_PUBLIC_DOMAIN=${lib.escapeShellArg publicDomain}
      export PYTHONPATH=${pythonModules}
      export HOMELAB_OPENSSL=${openssl}/bin/openssl
      exec ${python}/bin/python3 ${./homelab-secrets.py} "$@"
    '';
  };
in
symlinkJoin {
  name = "homelab-secrets";
  paths = [ command ];
  postBuild = ''
    mkdir -p "$out/share"
    ln -s ${pythonModules} "$out/share/homelab"
  '';
  passthru = { inherit python lanDomain publicDomain; };
}
