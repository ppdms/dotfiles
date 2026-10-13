{
  config,
  lib,
  pkgs,
  systemConfig,
  profileRoot,
  servicePrincipal ? null,
  ...
}:

{
  options.homelab.speedtest.enable = lib.mkEnableOption "rootless LibreSpeed canary UI (stateless)";

  config = lib.mkIf (config.homelab.speedtest.enable && servicePrincipal != null && servicePrincipal.name == "speedtest") {
    homelab.core.imageSources = [ ./containers/speedtest.container ];
    xdg.configFile."containers/systemd/speedtest.container".text =
      config.homelab.core.renderQuadlet ./containers/speedtest.container;
  };
}
