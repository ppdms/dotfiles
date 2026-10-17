{
  config,
  lib,
  pkgs,
  systemConfig,
  profileRoot,
  servicePrincipal ? null,
  ...
}:

let
  secretHelper = pkgs.callPackage ./tools/homelab-client.nix { };
  immichState = "${servicePrincipal.stateRoot}/immich";
  requireImmichState = pkgs.writeShellApplication {
    name = "homelab-immich-state";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.podman
    ];
    text = ''
      umask 077
      state=${lib.escapeShellArg immichState}
      install -d -m 0700 "$state"
      # The service requires a nonempty PostgreSQL version file in its state view.
      if [[ ! -s "$state/postgres/PG_VERSION" ]]; then
        echo "Immich start blocked: $state/postgres/PG_VERSION is missing or empty; refusing to initialize a replacement database." >&2
        exit 1
      fi
      # Immich images read secrets from env vars, not *_FILE. Assemble a
      # Quadlet EnvironmentFile consumed alongside the secret volume.
      secrets="''${XDG_RUNTIME_DIR:?}/homelab-secrets/immich"
      {
        printf 'POSTGRES_PASSWORD=%s\n' "$(cat "$secrets/postgres-password")"
        printf 'DB_USERNAME=%s\n' "$(cat "$secrets/db-username")"
        printf 'DB_PASSWORD=%s\n' "$(cat "$secrets/db-password")"
        printf 'DB_DATABASE_NAME=%s\n' "$(cat "$secrets/db-name")"
      } > "$secrets/immich.env"
      chmod 0600 "$secrets/immich.env"
      # Required named volumes must exist in this account's runtime.
      # Startup must not create empty replacement volumes or seed a database.
      for name in immich-postgres-data immich-redis-data immich_model-cache; do
        if ! podman volume exists "$name"; then
          echo "Immich start blocked: required named volume $name does not exist in this runtime; refusing an empty replacement volume." >&2
          exit 1
        fi
      done
      database=$(podman volume inspect --format '{{.Mountpoint}}' immich-postgres-data)
      if [[ ! -s "$database/PG_VERSION" ]]; then
        echo "Immich start blocked: $database/PG_VERSION is missing or empty in the immich-postgres-data volume; refusing a replacement database." >&2
        exit 1
      fi
    '';
  };
  renderQuadlet =
    source:
    lib.replaceStrings
      [ "@secret-helper@" "@state-helper@" ]
      [
        "${secretHelper}/bin/homelab-secrets"
        "${requireImmichState}/bin/homelab-immich-state"
      ]
      (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.immich.enable = lib.mkEnableOption "rootless Immich photo library";

  config = lib.mkIf (config.homelab.immich.enable && servicePrincipal != null && servicePrincipal.name == "immich") {
    homelab.core.imageSources = [
      ./containers/immich-database.container
      ./containers/immich-redis.container
      ./containers/immich-machine-learning.container
      ./containers/immich-server.container
      ./containers/immich-microservices.container
    ];
    xdg.configFile."containers/systemd/immich-database.container".text =
      renderQuadlet ./containers/immich-database.container;
    xdg.configFile."containers/systemd/immich-postgres-data.volume".text =
      config.homelab.core.renderConfig ./containers/immich-postgres-data.volume;
    xdg.configFile."containers/systemd/immich-redis.container".text =
      renderQuadlet ./containers/immich-redis.container;
    xdg.configFile."containers/systemd/immich-redis-data.volume".text =
      config.homelab.core.renderConfig ./containers/immich-redis-data.volume;
    xdg.configFile."containers/systemd/immich-model-cache.volume".text =
      config.homelab.core.renderConfig ./containers/immich-model-cache.volume;
    xdg.configFile."containers/systemd/immich-machine-learning.container".text =
      renderQuadlet ./containers/immich-machine-learning.container;
    xdg.configFile."containers/systemd/immich-server.container".text =
      renderQuadlet ./containers/immich-server.container;
    xdg.configFile."containers/systemd/immich-microservices.container".text =
      renderQuadlet ./containers/immich-microservices.container;

    home.activation.createImmichState = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg immichState}
    '';
  };
}
