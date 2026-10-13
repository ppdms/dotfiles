{
  config,
  lib,
  pkgs,
  systemConfig,
  profileRoot,
  servicePrincipal ? null,
  servicePrincipals,
  ...
}:

let
  credentialDirectory = "${config.xdg.dataHome}/keyring-credentials";
  credentialFile = "${credentialDirectory}/keyring-password.cred";

  startKeyring = pkgs.writeShellScript "start-gnome-keyring" ''
    exec ${pkgs.gnome-keyring}/bin/gnome-keyring-daemon \
      --foreground --unlock --components=secrets \
      --control-directory="$XDG_RUNTIME_DIR/keyring" \
      < "$CREDENTIALS_DIRECTORY/keyring-password"
  '';

  initializeKeyring = pkgs.writeShellApplication {
    name = "keyring-init";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      umask 077
      credential_file=${lib.escapeShellArg credentialFile}

      require_tpm_credential() {
        local key_type
        key_type=$(base64 --decode "$1" | od -An -N16 -tx1 | tr -d ' \n')
        # systemd's user-scoped host + TPM2 credential formats.
        case "$key_type" in
          ef4ac13679a9480ea7db68897f9f165d|adbc4ca3efb64201ba881b6f2e4095ea) ;;
          *)
            echo "Refusing a keyring credential that is not bound to the TPM and this user." >&2
            return 1
            ;;
        esac
      }

      if [[ -s "$credential_file" ]]; then
        require_tpm_credential "$credential_file"
        exit 0
      fi

      echo "The personal operator keyring requires its TPM-bound unlock credential at $credential_file; refusing to generate a replacement." >&2
      exit 1
    '';
  };
in
{
  options.homelab.keyring.enable = lib.mkEnableOption "the personal operator's standalone TPM-unlocked keyring";

  config = lib.mkIf config.homelab.keyring.enable {
    assertions = [
      {
        assertion = servicePrincipal == null && config.home.username == servicePrincipals.operator.username;
        message = "The personal keyring is available only to the configured operator, never to workload principals.";
      }
    ];
    home.packages = [
      pkgs.gnome-keyring
      pkgs.libsecret
      initializeKeyring
    ];

    services.gnome-keyring = {
      enable = true;
      components = [ "secrets" ];
    };

    # Terminal and SSH sessions use default.target rather than graphical targets.
    # systemd decrypts the machine-bound credential and feeds it to the daemon.
    systemd.user.services.gnome-keyring = {
      Unit.PartOf = lib.mkForce [ "default.target" ];
      Install.WantedBy = lib.mkForce [ "default.target" ];
      Service = {
        Type = "dbus";
        BusName = "org.freedesktop.secrets";
        ExecStart = lib.mkForce "${startKeyring}";
        LoadCredentialEncrypted = "keyring-password:${credentialFile}";
        RuntimeDirectory = "keyring";
        RuntimeDirectoryMode = "0700";
        UMask = "0077";
        Restart = lib.mkForce "on-failure";
        RestartSec = "2s";
      };
    };

    # Direct D-Bus activation through the user service, which supplies the unlock
    # credential. This avoids depending on a system GNOME/PAM installation.
    xdg.dataFile."dbus-1/services/org.freedesktop.secrets.service".text = ''
      [D-BUS Service]
      Name=org.freedesktop.secrets
      Exec=${pkgs.systemd}/bin/systemctl --user start gnome-keyring.service
      SystemdService=gnome-keyring.service
    '';

    home.activation.initializeKeyring =
      lib.hm.dag.entryBetween [ "reloadSystemd" ] [ "writeBoundary" ]
        ''
          run ${initializeKeyring}/bin/keyring-init
        '';
  };
}
