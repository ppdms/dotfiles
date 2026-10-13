{
  lib,
  writeShellApplication,
  writeText,
  systemd,
  openssl,
  coreutils,
  provisionInputs ? null,
  secrets,
  images,
  imageSpec,
  signaturePolicy,
  repository,
}:

let
  inputs = writeText "backrest-input-paths.json" (builtins.toJSON provisionInputs);
in
writeShellApplication {
  name = "homelab-backrest-provision";
  text = ''
    export PYTHONPATH=${secrets}/share/homelab
    export HOMELAB_OPENSSL=${openssl}/bin/openssl
    export HOMELAB_LAN_DOMAIN=${lib.escapeShellArg secrets.lanDomain}
    export HOMELAB_PUBLIC_DOMAIN=${lib.escapeShellArg secrets.publicDomain}
    export HOMELAB_SECRETS=${secrets}/bin/homelab-secrets
    export HOMELAB_SYSTEMCTL=${systemd}/bin/systemctl
    export HOMELAB_ENV=${coreutils}/bin/env
    export HOMELAB_SUDO=/run/wrappers/bin/sudo
    export HOMELAB_PYTHON=${secrets.python}/bin/python3
    export HOMELAB_IMAGES=${images}/bin/homelab-image-update
    export HOMELAB_IMAGE_SPEC=${imageSpec}
    export HOMELAB_SIGNATURE_POLICY=${signaturePolicy}
    export HOMELAB_REPOSITORY=${repository}/bin/homelab-backrest-repository
    ${lib.optionalString (provisionInputs != null) "export HOMELAB_INPUTS=${inputs}"}
    exec ${secrets.python}/bin/python3 ${./backrest-provision.py} "$@"
  '';
}
