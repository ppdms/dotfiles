#!/usr/bin/env bash

set -euo pipefail

force=false
if [[ "${1:-}" == "--force" ]]; then
    force=true
    shift
fi
if [[ $# -ne 0 ]]; then
    printf 'Usage: %s [--force]\n' "${0##*/}" >&2
    exit 2
fi

state_dir="${NIX_CONFIG_STATE_DIR:-${XDG_STATE_HOME:-"$HOME/.local/state"}/nix-config}"
identity="${NIX_CONFIG_INNER_IDENTITY:-${XDG_CONFIG_HOME:-"$HOME/.config"}/sops/age/outer-key.txt}"
policy="$state_dir/.sops.yaml"
profile_sops="$state_dir/profile.sops.yaml"
profile_json="$state_dir/profile.json"
vscode_config="$state_dir/vscode_ssh_config"
syncthing_config="$state_dir/syncthing/config.xml"

for path in "$identity" "$policy" "$profile_sops"; do
    [[ -f "$path" ]] || {
        printf 'Missing local input: %s\n' "$path" >&2
        exit 1
    }
done

umask 077
mkdir -p "$state_dir/syncthing"
tmp_dir="$(mktemp -d "$state_dir/.render.XXXXXX")"
cleanup() {
    rm -rf -- "$tmp_dir"
}
trap cleanup EXIT

export SOPS_AGE_KEY_FILE="$identity"
sops --config "$policy" --decrypt --output-type json "$profile_sops" |
    jq -er '.payload | fromjson' > "$tmp_dir/profile.json"
jq -er '.files.vscodeSshConfig' "$tmp_dir/profile.json" > "$tmp_dir/vscode_ssh_config"
jq -er '.files.syncthingConfig' "$tmp_dir/profile.json" > "$tmp_dir/syncthing.xml"

check_local_change() {
    local current=$1
    local rendered=$2

    if [[ -e "$current" ]] && ! cmp -s -- "$current" "$rendered" && [[ "$force" != true ]]; then
        printf 'Refusing to overwrite locally changed file: %s\n' "$current" >&2
        printf 'Run seal-profile.sh first, or rerun with --force.\n' >&2
        exit 1
    fi
}

check_local_change "$profile_json" "$tmp_dir/profile.json"
check_local_change "$vscode_config" "$tmp_dir/vscode_ssh_config"
check_local_change "$syncthing_config" "$tmp_dir/syncthing.xml"

install -m 600 "$tmp_dir/profile.json" "$profile_json"
install -m 600 "$tmp_dir/vscode_ssh_config" "$vscode_config"
install -m 600 "$tmp_dir/syncthing.xml" "$syncthing_config"

cat > "$state_dir/flake.nix" <<'EOF'
{
  description = "Local machine profile";

  outputs =
    { self }:
    let
      profileData = builtins.fromJSON (builtins.readFile ./profile.json);
    in
    {
      profiles = profileData.profiles;
      wireguard = profileData.wireguard;
    };
}
EOF
chmod 600 "$state_dir/flake.nix"

printf '%s\n' "$state_dir"
