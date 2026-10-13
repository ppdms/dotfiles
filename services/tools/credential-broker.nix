{
  lib,
  writeShellApplication,
  secrets,
  age,
  systemd,
  libsecret,
  openssl,
}:

writeShellApplication {
  name = "homelab-credential-broker";
  text = ''
    export HOMELAB_LAN_DOMAIN=${lib.escapeShellArg secrets.lanDomain}
    export HOMELAB_PUBLIC_DOMAIN=${lib.escapeShellArg secrets.publicDomain}
    export PYTHONPATH=${lib.escapeShellArg "${secrets}/share/homelab"}
    export HOMELAB_SYSTEMD_CREDS=${systemd}/bin/systemd-creds
    export HOMELAB_SECRET_TOOL=${libsecret}/bin/secret-tool
    export HOMELAB_AGE=${age}/bin/age
    export HOMELAB_OPENSSL=${openssl}/bin/openssl
    export HOMELAB_SUDO=/run/wrappers/bin/sudo
    exec ${secrets.python}/bin/python3 ${./credential_broker.py} "$@"
  '';
}
