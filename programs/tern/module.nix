{
  config,
  lib,
  pkgs,
  servicePrincipals,
  ...
}:

let
  cfg = config.programs.tern;
  webTokenFile = "%t/tern/web-token";
in
{
  options.programs.tern = {
    enable = lib.mkEnableOption "Tern terminal multiplexer";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      description = "Tern package; pinned by programs/tern/source.json.";
    };

    web = {
      enable = lib.mkEnableOption "the loopback `tern web serve` behind the ingress";

      port = lib.mkOption {
        type = lib.types.port;
        default = servicePrincipals.ports.omp;
        description = "Loopback port; the ingress vhosts and the uid firewall use the same one.";
      };

      credentials = lib.mkOption {
        type = lib.types.package;
        description = ''
          Required homelab-tern credential helper for the web service. The
          WebSocket token comes from the encrypted credential store (the same
          value nginx adds in front) and is written to %t/tern/web-token on tmpfs
          before every start.
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    systemd.user.services.tern-web = lib.mkIf cfg.web.enable {
      Unit = {
        Description = "Tern web terminal server for OMP";
        After = [ "network.target" ];
        # The first start after boot can race the credential broker; keep retrying.
        StartLimitIntervalSec = 0;
      };
      Service = {
        Type = "simple";
        ExecStartPre = "${cfg.web.credentials}/bin/homelab-tern web-token write ${webTokenFile}";
        ExecStart = "${cfg.package}/bin/tern web serve --listen 127.0.0.1:${toString cfg.web.port} --token ${webTokenFile}";
        Restart = "always";
        RestartSec = "5s";
        # Panes inherit this: omp and the user's tools must be found.
        Environment = [
          "PATH=%h/.local/bin:%h/.nix-profile/bin:/run/current-system/sw/bin"
        ];
      };
      Install.WantedBy = [ "default.target" ];
    };
  };
}
