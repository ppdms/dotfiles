{
  pkgs,
  systemConfig,
  self,
  ...
}:
{
  # Determinate Nix manages the Nix installation
  nix.enable = false;

  # Override fish package to disable tests due to upstream issue
  nixpkgs.overlays = [
    (final: prev: {
      fish = prev.fish.overrideAttrs (oldAttrs: {
        doCheck = false;
      });

      # Backrest's optional tray support crashes the nixpkgs Darwin linker.
      # The launchd service below only uses the headless server.
      backrest = prev.backrest.overrideAttrs (_: {
        tags = [ ];
      });
    })
  ];

  # The platform the configuration will be used on.
  nixpkgs.hostPlatform = "aarch64-darwin";

  system.configurationRevision = self.rev or self.dirtyRev or null;

  users.users.${systemConfig.system.username} = {
    name = systemConfig.system.username;
    home = systemConfig.system.homeDirectory;
  };

  system.primaryUser = systemConfig.system.username;

  security.pam.services.sudo_local.touchIdAuth = true;

  system.defaults.CustomUserPreferences = {
    "org.hammerspoon.Hammerspoon" = {
      MJConfigFile = "~/.config/nix/apps/hammerspoon/init.lua";
    };
    "com.microsoft.VSCode" = {
      ApplePressAndHoldEnabled = false;
    };
  };

  # Finder settings
  system.defaults.finder.ShowPathbar = false;
  system.defaults.finder.AppleShowAllExtensions = true;
  system.defaults.finder.FXDefaultSearchScope = "SCcf";
  system.defaults.finder.FXEnableExtensionChangeWarning = false;
  system.defaults.finder.NewWindowTarget = "Home";
  # system.defaults.finder.QuitMenuItem = true;
  system.defaults.finder.ShowExternalHardDrivesOnDesktop = false;
  system.defaults.finder.ShowRemovableMediaOnDesktop = false;
  system.defaults.finder._FXShowPosixPathInTitle = true;
  system.defaults.finder._FXSortFoldersFirst = true;
  system.defaults.finder._FXSortFoldersFirstOnDesktop = true;

  # Create restic wrapper and other system setup
  system.activationScripts.postActivation.text = ''
        # Create restic wrapper app for Full Disk Access
        # Must be a compiled binary (not shell script) for FDA to work
        RESTIC_WRAPPER_SRC=$(mktemp).c
        cat > "$RESTIC_WRAPPER_SRC" << 'EOF'
    #include <unistd.h>
    int main(int argc, char *argv[]) {
        argv[0] = "/etc/profiles/per-user/${systemConfig.system.username}/bin/restic";
        return execv(argv[0], argv);
    }
    EOF

        RESTIC_NEEDS_REBUILD=0
        if [ ! -d /Applications/ResticWrapper.app ]; then
          RESTIC_NEEDS_REBUILD=1
        elif [ ! -f /Applications/ResticWrapper.app/Contents/MacOS/ResticWrapper ]; then
          RESTIC_NEEDS_REBUILD=1
        elif file /Applications/ResticWrapper.app/Contents/MacOS/ResticWrapper | grep -q "shell script"; then
          # Replace shell script with compiled binary
          RESTIC_NEEDS_REBUILD=1
        fi

        if [ "$RESTIC_NEEDS_REBUILD" = "1" ]; then
          mkdir -p /Applications/ResticWrapper.app/Contents/MacOS
          clang -O2 -o /Applications/ResticWrapper.app/Contents/MacOS/ResticWrapper "$RESTIC_WRAPPER_SRC"
          chmod 755 /Applications/ResticWrapper.app/Contents/MacOS/ResticWrapper
          chown root:wheel /Applications/ResticWrapper.app/Contents/MacOS/ResticWrapper

          cat > /Applications/ResticWrapper.app/Contents/Info.plist << 'EOF'
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
        <key>CFBundleExecutable</key>
        <string>ResticWrapper</string>
        <key>CFBundleIdentifier</key>
        <string>com.restic.wrapper</string>
        <key>CFBundleName</key>
        <string>ResticWrapper</string>
        <key>CFBundleVersion</key>
        <string>1.0</string>
    </dict>
    </plist>
    EOF
          chown root:wheel /Applications/ResticWrapper.app/Contents/Info.plist
          chmod 644 /Applications/ResticWrapper.app/Contents/Info.plist

          echo "⚠️  Created/rebuilt /Applications/ResticWrapper.app - Please grant Full Disk Access in System Preferences > Privacy & Security"
        fi
        rm -f "$RESTIC_WRAPPER_SRC"

        # Create backrest wrapper app for Full Disk Access
        # Must be a compiled binary (not shell script) for FDA to work
        BACKREST_WRAPPER_SRC=$(mktemp).c
        cat > "$BACKREST_WRAPPER_SRC" << 'EOF'
    #include <unistd.h>
    int main(int argc, char *argv[]) {
        argv[0] = "/etc/profiles/per-user/${systemConfig.system.username}/bin/backrest";
        return execv(argv[0], argv);
    }
    EOF

        BACKREST_NEEDS_REBUILD=0
        if [ ! -d /Applications/BackrestWrapper.app ]; then
          BACKREST_NEEDS_REBUILD=1
        elif [ ! -f /Applications/BackrestWrapper.app/Contents/MacOS/BackrestWrapper ]; then
          BACKREST_NEEDS_REBUILD=1
        elif file /Applications/BackrestWrapper.app/Contents/MacOS/BackrestWrapper | grep -q "shell script"; then
          # Replace shell script with compiled binary
          BACKREST_NEEDS_REBUILD=1
        fi

        if [ "$BACKREST_NEEDS_REBUILD" = "1" ]; then
          mkdir -p /Applications/BackrestWrapper.app/Contents/MacOS
          clang -O2 -o /Applications/BackrestWrapper.app/Contents/MacOS/BackrestWrapper "$BACKREST_WRAPPER_SRC"
          chmod 755 /Applications/BackrestWrapper.app/Contents/MacOS/BackrestWrapper
          chown root:wheel /Applications/BackrestWrapper.app/Contents/MacOS/BackrestWrapper

          cat > /Applications/BackrestWrapper.app/Contents/Info.plist << 'EOF'
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
        <key>CFBundleExecutable</key>
        <string>BackrestWrapper</string>
        <key>CFBundleIdentifier</key>
        <string>com.backrest.wrapper</string>
        <key>CFBundleName</key>
        <string>BackrestWrapper</string>
        <key>CFBundleVersion</key>
        <string>1.0</string>
    </dict>
    </plist>
    EOF
          chown root:wheel /Applications/BackrestWrapper.app/Contents/Info.plist
          chmod 644 /Applications/BackrestWrapper.app/Contents/Info.plist

          echo "⚠️  Created/rebuilt /Applications/BackrestWrapper.app - Please grant Full Disk Access in System Preferences > Privacy & Security"
        fi
        rm -f "$BACKREST_WRAPPER_SRC"
  '';

  # Backrest backup service running as root
  launchd.daemons.backrest = {
    serviceConfig = {
      ProgramArguments = [ "/Applications/BackrestWrapper.app/Contents/MacOS/BackrestWrapper" ];
      EnvironmentVariables = {
        BACKREST_PORT = "127.0.0.1:9898";
        BACKREST_RESTIC_COMMAND = "/Applications/ResticWrapper.app/Contents/MacOS/ResticWrapper";
        BACKREST_CONFIG = "/Users/${systemConfig.system.username}/.local/state/backrest/config";
        BACKREST_DATA = "/Users/${systemConfig.system.username}/.local/state/backrest/data";
        HOME = "/var/root";
        RCLONE_CONFIG = "/var/root/.config/rclone/rclone.conf";
      };
      KeepAlive = true;
      RunAtLoad = true;
      ProcessType = "Background";
    };
  };

  homebrew = {
    enable = true;
    onActivation.cleanup = "zap";
    # Homebrew Bundle 4.7+ requires explicit confirmation/force when using
    # `--cleanup`; nix-darwin's `zap` maps to `--cleanup --zap`.
    onActivation.extraFlags = [ "--force-cleanup" ];

    taps = [
      "theseal/ssh-askpass"
    ];
    brews = [
      "aom"
      "aria2"
      "boost"
      "c-ares"
      "git-filter-repo"
      "hf"
      "imagemagick"
      "libde265"
      "libheif"
      "libssh2"
      "libtool"
      "m4"
      "ollama"
      "pandoc"
      "swiftlint"
      "swiftgen"
      "poppler"
      "tesseract"
      "ffmpeg"
      "pngpaste"
      "go"
      "lima"
      "colima"
      "docker"
      "neovim"
      "ripgrep"
      "age-plugin-se"
      "theseal/ssh-askpass/ssh-askpass"
    ];
    casks = [
      "antigravity-cli"
      "beeper"
      "cursor"
      "cursor-cli"
      "discord"
      "codexbar"
      "yubico-authenticator"
      "whatsapp"
      "basictex"
      "claude-code@latest"
      "flux-app"
      "zotero"
      "nextcloud"
      "texshop"
      "anki"
      "protonvpn"
      "iina"
      "volume-control"
      "proton-mail-bridge"
      "losslessswitcher"
      "obsidian"
      "maccy"
      "linearmouse"
      "winbox"
      "tailscale-app"
      "telegram"
      "cloudflare-warp"
      "kekaexternalhelper"
      "keka"
      "betterdisplay"
      "bitwarden"
      "rectangle"
      "visual-studio-code"
      "stats"
      "firefox"
      "ghostty"
      "hammerspoon"
      "google-chrome"
      "zed"
      "zoom"
      "codex"
      "chatgpt"
    ];
  };

  # Used for backwards compatibility. please read the changelog
  # before changing: `darwin-rebuild changelog`.
  system.stateVersion = "25.05";
}
