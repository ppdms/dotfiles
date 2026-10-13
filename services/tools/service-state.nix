{
  lib,
  writeShellApplication,
  secrets,
  coreutils,
  util-linux,
  acl,
  podman,
  systemd,
  age,
}:

writeShellApplication {
  name = "homelab-service-state";
  text = ''
    # /proc/self/fd bind targets pin validated inodes; libmount's newer
    # mount API rejects those magic links. Use mount(2), not path expansion.
    export LIBMOUNT_FORCE_MOUNT2=always
    export PYTHONPATH=${lib.escapeShellArg "${secrets}/share/homelab"}
    export HOMELAB_ENV=${coreutils}/bin/env
    export HOMELAB_CP=${coreutils}/bin/cp
    export HOMELAB_MOUNT=${util-linux}/bin/mount
    export HOMELAB_UMOUNT=${util-linux}/bin/umount
    export HOMELAB_SETFACL=${acl}/bin/setfacl
    export HOMELAB_PODMAN=${podman}/bin/podman
    export HOMELAB_SYSTEMCTL=${systemd}/bin/systemctl
    export HOMELAB_SYSTEMD_CREDS=${systemd}/bin/systemd-creds
    export HOMELAB_AGE=${age}/bin/age
    export HOMELAB_SUDO=/run/wrappers/bin/sudo
    exec ${secrets.python}/bin/python3 ${./service-state.py} "$@"
  '';
}
