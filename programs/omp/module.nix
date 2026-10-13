{
  config,
  lib,
  pkgs,
  systemConfig,
  ...
}:

let
  cfg = config.programs.omp;
  yaml = pkgs.formats.yaml { };
  profile = "${config.xdg.stateHome}/nix/profiles/omp";
  updater = pkgs.callPackage ./update.nix { };
  dispatcher = pkgs.writeShellScriptBin "omp" ''
    if [ "''${1-}" = update ]; then
      shift
      exec ${updater}/bin/homelab-omp-update "$@" \
        --checkout ${lib.escapeShellArg cfg.checkout} \
        --profile ${lib.escapeShellArg profile}
    fi
    exec ${lib.escapeShellArg "${profile}/bin/omp"} "$@"
  '';
  commonSettings = {
    symbolPreset = "nerd";
    theme.dark = "titanium";
    composer.shape = "claude";
    edit.mode = "apply_patch";
    display = {
      showTokenUsage = true;
      cacheMissMarker = true;
    };
    statusLine = {
      preset = "full";
      separator = "powerline-thin";
    };
    extendedContext = false;
    claudeResets.autoRedeem = "no";
  };
  installSettings = pkgs.writeShellScript "omp-install-settings" ''
    set -eu
    directory="$HOME/.omp/agent"
    ${pkgs.coreutils}/bin/mkdir -p "$directory"
    temporary="$(${pkgs.coreutils}/bin/mktemp "$directory/.config.yml.XXXXXX")"
    trap '${pkgs.coreutils}/bin/rm -f "$temporary"' EXIT
    ${pkgs.coreutils}/bin/install -m 600 "$1" "$temporary"
    # Replace config symlinks without writing through them into a checkout.
    # Models, authentication, and sessions are deliberately not managed.
    ${pkgs.coreutils}/bin/mv -fT "$temporary" "$directory/config.yml"
    trap - EXIT
  '';
in
{
  options.programs.omp = {
    enable = lib.mkEnableOption "OMP coding agent";

    managePackage = lib.mkOption {
      type = lib.types.bool;
      default = pkgs.stdenv.hostPlatform.isLinux;
      description = ''
        Install the isolated native OMP profile and updater. Disable for
        settings-only management that preserves an existing OMP installation.
      '';
    };

    package = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default =
        if pkgs.stdenv.hostPlatform.system == "x86_64-linux" then
          pkgs.callPackage ../../tools/omp.nix { }
        else
          null;
      description = ''
        OMP package used to initialize the isolated OMP profile. Full Home
        Manager activations reconcile that profile to this package.
      '';
    };

    checkout = lib.mkOption {
      type = lib.types.str;
      default = systemConfig.system.checkout;
      description = "Writable flake checkout used by omp update.";
    };

    settings = lib.mkOption {
      type = lib.types.nullOr yaml.type;
      default = null;
      description = ''
        Settings copied to ~/.omp/agent/config.yml on activation. The file
        stays writable so OMP can update it at runtime. The next activation
        restores the declared settings. Leave null to let OMP manage the file.
      '';
    };
  };

  config = lib.mkMerge [
    {
      programs.omp = {
        enable = lib.mkDefault true;
        settings = lib.mkDefault (
          lib.recursiveUpdate commonSettings (systemConfig.applications.omp or { })
        );
      };
    }
    (lib.mkIf cfg.enable {
      assertions = [
        {
          assertion =
            !cfg.managePackage || (pkgs.stdenv.hostPlatform.system == "x86_64-linux" && cfg.package != null);
          message = "Managed native OMP packaging supports x86_64-linux only; use settings-only management elsewhere.";
        }
      ];
      home.packages = lib.mkIf cfg.managePackage [ dispatcher ];

      home.activation.ompProfile = lib.mkIf cfg.managePackage (
        lib.hm.dag.entryBetween [ "installPackages" ] [ "writeBoundary" ] ''
          run ${pkgs.coreutils}/bin/mkdir -p ${lib.escapeShellArg (builtins.dirOf profile)}
          run ${pkgs.util-linux}/bin/flock ${lib.escapeShellArg "${profile}.update.lock"} \
            ${pkgs.nix}/bin/nix-env --profile ${lib.escapeShellArg profile} \
            --set ${lib.escapeShellArg (toString cfg.package)}
        ''
      );

      home.activation.ompSettings = lib.mkIf (cfg.settings != null) (
        lib.hm.dag.entryAfter [ "writeBoundary" ] ''
          run ${installSettings} ${yaml.generate "omp-config.yml" cfg.settings}
        ''
      );
    })
  ];
}
