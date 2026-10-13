{
  config,
  lib,
  pkgs,
  systemConfig,
  servicePrincipal ? null,
  ...
}:

let
  cfg = config.homelab.secrets;
  secrets = pkgs.callPackage ./tools/homelab-secrets.nix {
    lanDomain = systemConfig.homelab.lanDomain;
    publicDomain = systemConfig.homelab.publicDomain;
  };
  secretsExport = pkgs.callPackage ./tools/secrets-export.nix {
    inherit secrets;
    stateRoot = servicePrincipal.stateRoot;
  };
  persistMutable = pkgs.writeShellApplication {
    name = "homelab-persist-mutable-secrets";
    text = lib.concatMapStringsSep "\n" (component: ''
      ${secrets}/bin/homelab-secrets persist ${lib.escapeShellArg component}
    '') (lib.unique cfg.mutableComponents);
  };
in
{
  options.homelab.secrets.mutableComponents = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [ ];
    description = "This principal's mutable runtime credentials to persist through the scoped broker.";
  };

  config = lib.mkIf config.homelab.core.enable (lib.mkMerge [
    {
      assertions = [
        {
          assertion = lib.all (component: builtins.elem component servicePrincipal.components) cfg.mutableComponents;
          message = "A principal may persist only its own mutable credential components.";
        }
      ];
    }
    (lib.mkIf (cfg.mutableComponents != [ ]) {
      # Only mutable owners enroll this timer. The broker authenticates the
      # caller's Unix UID; no workload runs or waits for an operator keyring.
      systemd.user.services.homelab-secrets-persist = {
        Unit = {
          Description = "Persist application credential updates";
          PartOf = [ "homelab.target" ];
        };
        Service = {
          Type = "oneshot";
          ExecStart = "${persistMutable}/bin/homelab-persist-mutable-secrets";
          UMask = "0077";
        };
      };
      systemd.user.timers.homelab-secrets-persist = {
        Unit.PartOf = [ "homelab.target" ];
        Timer = {
          OnActiveSec = "1min";
          OnUnitActiveSec = "1min";
        };
        Install.WantedBy = [ "homelab.target" ];
      };
    })
    (lib.mkIf (servicePrincipal.name == "secrets-export") {
      # Export authority receives original-recipient ciphertext, not the
      # application read scopes. Keep the previous exports on partial failure.
      systemd.user.services.homelab-secrets-export = {
        Unit = {
          Description = "Export encrypted credential backups";
          PartOf = [ "homelab.target" ];
        };
        Service = {
          Type = "oneshot";
          ExecStart = "${secretsExport}/bin/homelab-secrets-export";
          UMask = "0077";
        };
      };
      systemd.user.timers.homelab-secrets-export = {
        Unit.PartOf = [ "homelab.target" ];
        Timer = {
          OnCalendar = "01:30 ${systemConfig.homelab.timeZone}";
          Persistent = true;
        };
        Install.WantedBy = [ "homelab.target" ];
      };
    })
  ]);
}
