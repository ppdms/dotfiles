#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
    cat <<'EOF'
Usage: rebuild.sh [--update] [--no-switch] [--platform darwin|nixos] [--state-dir PATH]

Render the selected sealed profile, then switch the selected host.
  --update       Update the public platform lock before rebuilding. On Linux,
                 also select the latest already-approved local image-feed HEAD.
                 The private profile override is never written to public locks.
  --no-switch    Render (and optionally update) without activating anything.
  --platform     Selected platform (default: detected from the operating system).
  --state-dir    Private state root (default: NIX_CONFIG_STATE_DIR or XDG state).
  -h, --help     Show this help.

Linux prepares STATE/deployment#<selected hostname> for manual review. A
passworded switch approves its immutable code/profile snapshot for root's
stable-package updater; the timer never consumes STATE or a user checkout.
Darwin builds this repository's root flake with the rendered profile input.
EOF
}
die() { printf 'rebuild: %s\n' "$*" >&2; exit 1; }
require_value() { [[ $# -ge 2 && -n "$2" ]] || die "$1 requires a value"; }

script_dir="$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$script_dir/profile-bootstrap.sh"
state_dir="${NIX_CONFIG_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/nix-config}"
platform=""
update=0
no_switch=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --update) update=1; shift ;;
        --no-switch) no_switch=1; shift ;;
        --platform) require_value "$@"; platform="$2"; shift 2 ;;
        --state-dir) require_value "$@"; state_dir="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown argument: $1" ;;
    esac
done
if [[ -z "$platform" ]]; then
    case "$(uname -s)" in
        Darwin) platform=darwin ;;
        Linux) platform=nixos ;;
        *) die 'Unsupported operating system; select --platform explicitly' ;;
    esac
fi
[[ "$platform" == darwin || "$platform" == nixos ]] || die 'Expected --platform darwin or nixos'
[[ "$state_dir" == /* ]] || die '--state-dir must be absolute'
if [[ "$platform" == nixos ]]; then
    prepare_args=(--state-dir "$state_dir")
    (( ! update )) || prepare_args+=(--update)
    bash "$script_dir/prepare-deployment.sh" "${prepare_args[@]}"
else
    profile_run render --platform "$platform" --state-dir "$state_dir" >/dev/null
fi
hostname="$(profile_run query --platform "$platform" --state-dir "$state_dir" --field hostname)"
checkout="$(profile_run query --platform "$platform" --state-dir "$state_dir" --field checkout)"
[[ "$hostname" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$ ]] || die 'Invalid selected hostname'
[[ "$checkout" == /* && -d "$checkout" ]] || die 'Selected checkout is not an existing absolute directory'
public_flake="$(profile_git_source_url "$checkout")"
checkout="$(CDPATH= cd -P -- "$checkout" && pwd -P)"

if [[ "$platform" == darwin ]]; then
    profile_input="$(profile_run query --platform darwin --state-dir "$state_dir" --field input)"
    profile_url="$(profile_run python -c 'import sys, urllib.parse; print("path:" + urllib.parse.quote(sys.argv[1], safe="/"))' "$profile_input")"
    [[ -f "$checkout/flake.nix" ]] || die 'Selected checkout has no root flake'
    # Keep the existing private GitHub-token workflow; never put the token in
    # command arguments, the public checkout, or a lock file.
    if (( update || ! no_switch )); then
        github_token="$(sops --decrypt --extract '["github_token"]' "$state_dir/secrets.sops.yaml")"
        [[ -n "$github_token" && "$github_token" != *$'\n'* && "$github_token" != *$'\r'* ]] || die 'Invalid private GitHub token'
        export NIX_CONFIG="${NIX_CONFIG:+$NIX_CONFIG$'\n'}access-tokens = github.com=$github_token"
        unset github_token
    fi
else
    [[ -f "$checkout/flake.nix" ]] || die 'Selected checkout has no public flake'
    if (( ! no_switch )); then
        [[ -f "$state_dir/deployment/flake.nix" ]] || die 'Missing private server deployment wrapper'
    fi
fi
if [[ "$platform" == darwin ]] && (( update )); then
    # Update with the public placeholder, not the real profile override.
    nix flake update --flake "$public_flake"
fi
local_packages="$state_dir/local-packages"
if [[ "$platform" == darwin ]] && [[ -x "$local_packages/refresh.sh" ]]; then
    # The private local-packages input may refresh its own pins here;
    # LOCAL_PACKAGES_UPDATE=1 only for `rebuild --update`. Nothing
    # about its contents is tracked in the public checkout.
    LOCAL_PACKAGES_UPDATE="$update" "$local_packages/refresh.sh" ||
        die 'Local packages refresh failed'
fi
# A switch that changes any of these units restarts it, and each refuses to run
# while Backrest's container holds the state paths it adopts. When the switch
# would restart one, stop Backrest first and start it again afterwards.
state_units=(
    homelab-service-state.service
    homelab-service-state-protect.service
    homelab-backup-access.service
    homelab-runtime-images.service
)
backrest_stopped=0
backrest_ctl() { sudo systemctl --user -M homelab-backrest@ "$@"; }
restore_backrest() {
    (( backrest_stopped )) || return 0
    backrest_stopped=0
    printf 'rebuild: starting Backrest again\n' >&2
    backrest_ctl start backrest.service ||
        { printf 'rebuild: Backrest did not start; run: sudo systemctl --user -M homelab-backrest@ start backrest.service\n' >&2; return 1; }
}
switch_nixos() {
    local flake="path:$state_dir/deployment" system unit changed=0 status=0
    # Build first, without privileges: it can outlast sudo's credential cache.
    system="$(nix build --no-link --print-out-paths --no-write-lock-file \
        "$flake#nixosConfigurations.$hostname.config.system.build.toplevel")"
    sudo -k -v
    for unit in "${state_units[@]}"; do
        cmp -s "/run/current-system/etc/systemd/system/$unit" "$system/etc/systemd/system/$unit" || changed=1
    done
    if (( changed )) && [[ "$(backrest_ctl is-active backrest.service || true)" == active ]]; then
        printf 'rebuild: state units change; stopping Backrest for the switch (a running backup is interrupted)\n' >&2
        # Armed before the stop so an interrupt or failure still restores it.
        backrest_stopped=1
        trap restore_backrest EXIT
        backrest_ctl stop backrest.service
    fi
    # Activate the toplevel built above directly. `nixos-rebuild switch`
    # would evaluate the flake a second time (tens of seconds) only to build
    # this same output, then do exactly these two steps: register the system
    # profile generation, then run switch-to-configuration.
    sudo nix-env --profile /nix/var/nix/profiles/system --set "$system" || status=$?
    if (( ! status )); then
        sudo "$system/bin/switch-to-configuration" switch || status=$?
    fi
    restore_backrest || (( status )) || status=1
    return "$status"
}
if (( ! no_switch )); then
    if [[ "$platform" == darwin ]]; then
        darwin_args=(
            --flake "$public_flake#$hostname"
            --override-input profile "$profile_url"
            --no-write-lock-file
        )
        if [[ -f "$local_packages/package.nix" ]]; then
            darwin_args+=(--override-input local-packages "path:$local_packages")
        fi
        sudo --preserve-env=NIX_CONFIG darwin-rebuild switch \
            "${darwin_args[@]}"
    else
        switch_nixos
    fi
fi
