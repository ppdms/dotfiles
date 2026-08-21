{
  pkgs,
  config,
  systemConfig,
  wireguardConfig,
  ...
}:
{
  # Bootloader.
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;

  # Use latest kernel.
  boot.kernelPackages = pkgs.linuxPackages_latest;

  networking.hostName = systemConfig.system.hostname;
  # networking.wireless.enable = true;  # Enables wireless support via wpa_supplicant.

  # Configure network proxy if necessary
  # networking.proxy.default = "http://user:password@proxy:port/";
  # networking.proxy.noProxy = "127.0.0.1,localhost,internal.domain";

  # Enable networking
  networking.networkmanager.enable = true;

  # WireGuard configuration from the encrypted profile.
  networking.wg-quick.interfaces = wireguardConfig.wireguard;

  # Set your time zone.
  time.timeZone = "Europe/Athens";

  # Select internationalisation properties.
  i18n.defaultLocale = "en_US.UTF-8";

  i18n.extraLocaleSettings = {
    LC_ADDRESS = "el_GR.UTF-8";
    LC_IDENTIFICATION = "el_GR.UTF-8";
    LC_MEASUREMENT = "el_GR.UTF-8";
    LC_MONETARY = "el_GR.UTF-8";
    LC_NAME = "el_GR.UTF-8";
    LC_NUMERIC = "el_GR.UTF-8";
    LC_PAPER = "el_GR.UTF-8";
    LC_TELEPHONE = "el_GR.UTF-8";
    LC_TIME = "el_GR.UTF-8";
  };

  # Enable the X11 windowing system.
  services.xserver.enable = true;
  services.spice-vdagentd.enable = true;
  services.qemuGuest.enable = true;

  # Enable the GNOME Desktop Environment.
  services.displayManager.gdm.enable = true;
  services.desktopManager.gnome.enable = true;
  services.gnome.gnome-browser-connector.enable = true;
  services.gnome.gnome-keyring.enable = true;

  # Enable SSH server
  services.openssh.enable = true;

  # Manage Syncthing manually with systemd (not using NixOS module to avoid dependency issues)

  # Service to initialize Syncthing config with authentication and TLS
  systemd.services.syncthing-init = {
    description = "Initialize Syncthing configuration";
    wantedBy = [ "syncthing.service" ];
    before = [ "syncthing.service" ];
    serviceConfig = {
      Type = "oneshot";
      # Need root to create bind mounts
    };
    environment = {
      HOME = "/home/${systemConfig.system.username}";
    };
    script = ''
      CONFIG_DIR="/home/${systemConfig.system.username}/.local/state/syncthing"
      CONFIG_FILE="$CONFIG_DIR/config.xml"
      PROFILE_SYNCTHING_DIR="${systemConfig.system.homeDirectory}/.local/state/nix-config/syncthing"

      # Wait for sops secrets to be available (key and cert only)
      while [ ! -f "${config.sops.secrets.syncthing_key_pem.path}" ] || [ ! -f "${config.sops.secrets.syncthing_cert_pem.path}" ]; do
        sleep 1
      done

      # Ensure config directory exists
      mkdir -p "$CONFIG_DIR"
      chown ${systemConfig.system.username}:users "$CONFIG_DIR"
      chmod 700 "$CONFIG_DIR"

      # Always replace key.pem and cert.pem from sops (force overwrite)
      KEY_PEM_FILE="$CONFIG_DIR/key.pem"
      CERT_PEM_FILE="$CONFIG_DIR/cert.pem"

      cp -f "${config.sops.secrets.syncthing_key_pem.path}" "$KEY_PEM_FILE"
      chmod 600 "$KEY_PEM_FILE"
      chown ${systemConfig.system.username}:users "$KEY_PEM_FILE"

      cp -f "${config.sops.secrets.syncthing_cert_pem.path}" "$CERT_PEM_FILE"
      chmod 600 "$CERT_PEM_FILE"
      chown ${systemConfig.system.username}:users "$CERT_PEM_FILE"

      # Remove existing config.xml if it's not a symlink, then create bind mount
      if [ -f "$PROFILE_SYNCTHING_DIR/config.xml" ]; then
        # Unmount if already mounted
        if ${pkgs.util-linux}/bin/mountpoint -q "$CONFIG_FILE" 2>/dev/null; then
          ${pkgs.util-linux}/bin/umount "$CONFIG_FILE"
        fi
        # Remove any existing file/symlink
        if [ -e "$CONFIG_FILE" ]; then
          rm -f "$CONFIG_FILE"
        fi
        # Create empty file as mount point
        touch "$CONFIG_FILE"
        chown ${systemConfig.system.username}:users "$CONFIG_FILE"
        # Bind mount the config file so Syncthing writes directly to the source
        ${pkgs.util-linux}/bin/mount --bind "$PROFILE_SYNCTHING_DIR/config.xml" "$CONFIG_FILE"
      else
        echo "Error: config.xml not found at $PROFILE_SYNCTHING_DIR/config.xml"
        exit 1
      fi
    '';
  };

  # Syncthing service
  # Based on official systemd service file:
  # https://github.com/syncthing/syncthing/blob/main/etc/linux-systemd/system/syncthing%40.service
  systemd.services.syncthing = {
    description = "Syncthing - Open Source Continuous File Synchronization";
    documentation = [ "man:syncthing(1)" ];
    after = [
      "network.target"
      "syncthing-init.service"
    ];
    requires = [ "syncthing-init.service" ];
    wantedBy = [ "multi-user.target" ];
    startLimitIntervalSec = 60;
    startLimitBurst = 4;

    serviceConfig = {
      User = systemConfig.system.username;
      ExecStart = "${pkgs.syncthing}/bin/syncthing serve --no-browser --no-restart";
      Restart = "on-failure";
      RestartSec = 1;
      SuccessExitStatus = [
        3
        4
      ];
      RestartForceExitStatus = [
        3
        4
      ];

      # Hardening (from official service file)
      ProtectSystem = "full";
      PrivateTmp = true;
      SystemCallArchitectures = "native";
      MemoryDenyWriteExecute = true;
      NoNewPrivileges = true;

      # Additional hardening options
      # ProtectHome removed to allow access to user home directory
      PrivateDevices = false;
      ProtectKernelTunables = true;
      ProtectControlGroups = true;
      RestrictRealtime = true;
      RestrictNamespaces = true;
      LockPersonality = true;

      # Elevated permissions to sync ownership
      # See https://docs.syncthing.net/advanced/folder-sync-ownership
      AmbientCapabilities = "CAP_CHOWN CAP_FOWNER";

      # Environment
      Environment = [
        "HOME=/home/${systemConfig.system.username}"
        "STHOMEDIR=/home/${systemConfig.system.username}/.local/state/syncthing"
        "STLOGFORMATTIMESTAMP="
        "STLOGFORMATLEVELSTRING=false"
        "STLOGFORMATLEVELSYSLOG=true"
      ];
    };
  };

  # Configure keymap in X11
  services.xserver.xkb = {
    layout = "us";
    variant = "";
  };

  # Enable CUPS to print documents.
  services.printing.enable = true;

  # Enable sound with pipewire.
  services.pulseaudio.enable = false;
  security.rtkit.enable = true;
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    alsa.support32Bit = true;
    pulse.enable = true;
    # If you want to use JACK applications, uncomment this
    #jack.enable = true;

    # use the example session manager (no others are packaged yet so this is enabled by default,
    # no need to redefine it in your config for now)
    #media-session.enable = true;
  };

  # Enable automatic login for the user.
  services.displayManager.autoLogin.enable = true;
  services.displayManager.autoLogin.user = systemConfig.system.username;

  # Workaround for GNOME autologin: https://github.com/NixOS/nixpkgs/issues/103746#issuecomment-945091229
  systemd.services."getty@tty1".enable = false;
  systemd.services."autovt@tty1".enable = false;

  # Disable all power management and sleep/suspend
  powerManagement.enable = false;

  systemd.targets.sleep.enable = false;
  systemd.targets.suspend.enable = false;
  systemd.targets.hibernate.enable = false;
  systemd.targets.hybrid-sleep.enable = false;

  # Configure systemd-logind to ignore all power events
  services.logind.settings.Login = {
    HandleLidSwitch = "ignore";
    HandleLidSwitchDocked = "ignore";
    HandlePowerKey = "ignore";
    HandleSuspendKey = "ignore";
    HandleHibernateKey = "ignore";
    IdleAction = "ignore";
  };

  # Disable GNOME Display Manager auto-suspend
  services.displayManager.gdm.autoSuspend = false;

  # Install firefox.
  programs.firefox.enable = true;

  environment.systemPackages = with pkgs; [
    spice-vdagent
    gnome-tweaks
    vscode
    firefox
    syncthing
    kitty
    obsidian
    vlc
  ];

  system.stateVersion = "25.05"; # Did you read the comment?
}
