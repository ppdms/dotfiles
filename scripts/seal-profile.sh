#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 0 ]]; then
    printf 'Usage: %s\n' "${0##*/}" >&2
    exit 2
fi

state_dir="${NIX_CONFIG_STATE_DIR:-${XDG_STATE_HOME:-"$HOME/.local/state"}/nix-config}"
identity="${NIX_CONFIG_INNER_IDENTITY:-${XDG_CONFIG_HOME:-"$HOME/.config"}/sops/age/outer-key.txt}"
policy="$state_dir/.sops.yaml"
profile_sops="$state_dir/profile.sops.yaml"
profile_json="$state_dir/profile.json"
vscode_config="$state_dir/vscode_ssh_config"
syncthing_config="$state_dir/syncthing/config.xml"

for path in "$identity" "$policy" "$profile_json" "$vscode_config" "$syncthing_config"; do
    [[ -f "$path" ]] || {
        printf 'Missing local input: %s\n' "$path" >&2
        exit 1
    }
done

umask 077
tmp_dir="$(mktemp -d "$state_dir/.seal.XXXXXX")"
cleanup() {
    rm -rf -- "$tmp_dir"
}
trap cleanup EXIT

jq \
    --rawfile vscode "$vscode_config" \
    --rawfile syncthing "$syncthing_config" \
    '.files.vscodeSshConfig = $vscode | .files.syncthingConfig = $syncthing' \
    "$profile_json" > "$tmp_dir/profile.json"

export SOPS_AGE_KEY_FILE="$identity"
jq -n --rawfile payload "$tmp_dir/profile.json" '{ payload: $payload }' |
    sops \
        --encrypt \
        --input-type json \
        --output-type yaml \
        --filename-override profile.sops.yaml \
        --config "$policy" \
        --output "$tmp_dir/profile.sops.yaml" \
        /dev/stdin

install -m 600 "$tmp_dir/profile.json" "$profile_json"
install -m 600 "$tmp_dir/profile.sops.yaml" "$profile_sops"
printf 'Updated %s\n' "$profile_sops"
