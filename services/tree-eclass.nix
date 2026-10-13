{
  config,
  lib,
  pkgs,
  systemConfig,
  servicePrincipal ? null,
  ...
}:

let
  isDev = servicePrincipal.name == "tree-dev";
  leaf = if isDev then "tree-eclass-dev" else "tree-eclass";
  state = "${servicePrincipal.stateRoot}/${leaf}";
  helperName = if isDev then "homelab-tree-dev-state" else "homelab-tree-state";
  secretHelper = pkgs.callPackage ./tools/homelab-secrets.nix {
    lanDomain = systemConfig.homelab.lanDomain;
    publicDomain = systemConfig.homelab.publicDomain;
  };
  originsArgument = lib.escapeShellArg (builtins.toJSON {
    hosts = [
      "uni.${systemConfig.homelab.lanDomain}"
      "uni.${systemConfig.homelab.publicDomain}"
      "127.0.0.1"
      "localhost"
    ];
    origins = [
      "https://uni.${systemConfig.homelab.lanDomain}"
      "https://uni.${systemConfig.homelab.publicDomain}"
    ];
  });
  requireTreeState = pkgs.writeShellApplication {
    name = helperName;
    runtimeInputs = [ pkgs.coreutils pkgs.podman ];
    text = ''
      umask 077
      state=${lib.escapeShellArg state}
      # Each role requires its own identity files. Neither may traverse the other's home
      # or manufacture a new session, access code, database or object store.
      if [[ ! -s "$state/session" || ! -s "$state/code" ||
            ! -s "$state/jobs/sqlite.db" || ! -d "$state/objects" ]]; then
        echo "Tree start blocked: session, code, SQLite jobs database and objects directory are required under $state; refusing a replacement identity/store." >&2
        exit 1
      fi
      secrets="''${XDG_RUNTIME_DIR:?}/homelab-secrets"
      runtime="$secrets/tree-eclass-runtime"
      install -d -m 0700 "$runtime"
      ${if isDev then ''
        branch=$(cat "$state/branch")
        release=$(cat "$state/rev")
        if [[ -z "$branch" || -z "$release" ]]; then
          echo "Tree dev start blocked: use tree-dev up to stage a manual branch and revision." >&2
          exit 1
        fi
      '' else ''
        branch=""
        release=$(podman image inspect --format '{{ index .Labels "org.opencontainers.image.revision" }}' localhost/tree-eclass:stable 2>/dev/null || true)
        if [[ -z "$release" ]]; then release="da29b64"; fi
      ''}
      ${pkgs.python3}/bin/python3 - "$secrets/tree-eclass/tree-eclass/provider-keys" "$state/session" "$state/code" "$release" "$branch" "$runtime/application.json" ${originsArgument} ${if isDev then "dev" else "production"} <<'EOF'
      import json, sys
      pkf, sf, cf, release, branch, out, origins_json, role = sys.argv[1:]
      origins = json.loads(origins_json)
      with open(pkf) as f:
          keys = json.load(f)
      with open(sf) as f:
          session = f.read().strip()
      with open(cf) as f:
          code = f.read().strip()
      keys = {k: v for k, v in keys.items() if not k.startswith("_")}
      cfg = {
          "allowed_hosts": origins["hosts"],
          "allowed_origins": origins["origins"],
          "sqlite_path": "/jobs/sqlite.db",
          "objects_root": "/data/objects",
          "address": "0.0.0.0:8001",
          "mode": "stable",
          "release": release,
          "session": session,
          "code": code,
          "temp": "/jobs",
          "external_workers": role == "production",
          "provider_keys": keys,
          "frontend_dir": "/app/frontend",
      }
      if role == "dev":
          cfg["storage_namespace"] = f"tree-eclass:development:{branch}:"
      with open(out, "w") as f:
          json.dump(cfg, f)
      EOF
      chmod 0600 "$runtime/application.json"
      # keep-id maps the image payload UID 10001 to this Unix account. Stage
      # ordinary private account-owned files, without subordinate-UID chown.
      staged=$(mktemp "$state/.application.XXXXXX")
      trap 'rm -f -- "$staged"' EXIT
      cp -- "$runtime/application.json" "$staged"
      chmod 0600 "$staged"
      mv -T -- "$staged" "$state/jobs/application.json"
      trap - EXIT
    '';
  };
  renderQuadlet = source: lib.replaceStrings
    [ "@secret-helper@" "@state-helper@" ]
    [
      "${secretHelper}/bin/homelab-secrets"
      "${requireTreeState}/bin/${helperName}"
    ]
    (config.homelab.core.renderQuadlet source);
in
{
  options.homelab.tree-eclass.enable = lib.mkEnableOption "this principal's Tree production or manual development runtime";

  config = lib.mkIf (config.homelab.tree-eclass.enable && servicePrincipal != null) (lib.mkMerge [
    (lib.mkIf (servicePrincipal.name == "tree-eclass") {
      xdg.configFile."containers/systemd/tree-eclass.container".text =
        renderQuadlet ./containers/tree-eclass.container;
      xdg.configFile."containers/systemd/tree-eclass-migrate.container".text =
        renderQuadlet ./containers/tree-eclass-migrate.container;
    })
    (lib.mkIf isDev {
      xdg.configFile."containers/systemd/tree-eclass-dev.container".text =
        renderQuadlet ./containers/tree-eclass-dev.container;
      xdg.configFile."containers/systemd/tree-eclass-dev-migrate.container".text =
        renderQuadlet ./containers/tree-eclass-dev-migrate.container;
    })
  ]);
}
