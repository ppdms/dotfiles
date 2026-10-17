#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
    cat <<'EOF'
Usage: rebuild.sh [--update] [--no-switch] [--only PRINCIPAL] [--platform darwin|nixos] [--state-dir PATH]

Render the selected sealed profile, then switch the selected host.
  --update       Update the public platform lock before rebuilding. On Linux,
                 also select the latest already-approved local image-feed HEAD.
                 The private profile override is never written to public locks.
  --no-switch    Render (and optionally update) without activating anything.
  --only P       NixOS only: assert the switch touches only principal P's
                 Home Manager account (e.g. --only readeck); refuses when
                 anything outside it changed. Without --only, the same
                 check runs automatically: when exactly one account
                 changed, the switch is scoped to it and says so.
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
only=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --update) update=1; shift ;;
        --no-switch) no_switch=1; shift ;;
        --only) require_value "$@"; only="$2"; shift 2 ;;
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
if [[ -n "$only" ]]; then
    [[ "$platform" == nixos ]] || die '--only is supported on NixOS only'
    [[ "$only" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || die "Invalid principal name: $only"
fi
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
scope_changed_hm=()
scope_changed_other=()
detect_switch_scope() {
    # Classify the restart-relevant difference between the running system
    # and a built toplevel: home-manager unit files (one per account) go to
    # scope_changed_hm, everything else (other systemd unit files,
    # /etc/homelab payloads, kernel/initrd/systemd links) to
    # scope_changed_other. Symlinks are compared by target.
    local system="$1" current="${HOMELAB_CURRENT_SYSTEM:-/run/current-system}"
    local rel root
    scope_changed_hm=()
    scope_changed_other=()
    for root in etc/systemd/system etc/homelab; do
        [[ -d "$current/$root" || -d "$system/$root" ]] || continue
        while IFS= read -r rel; do
            if [[ -L "$current/$root/$rel" || -L "$system/$root/$rel" ]]; then
                [[ "$(readlink "$current/$root/$rel" 2>/dev/null)" == "$(readlink "$system/$root/$rel" 2>/dev/null)" ]] && continue
            elif cmp -s "$current/$root/$rel" "$system/$root/$rel" 2>/dev/null; then
                continue
            fi
            if [[ "$root" == etc/systemd/system ]]; then
                case "$rel" in
                    home-manager-*.service|*/home-manager-*.service) scope_changed_hm+=("$rel"); continue ;;
                esac
            fi
            scope_changed_other+=("$root/$rel")
        done < <(
            (cd "$current/$root" 2>/dev/null && find . \( -type f -o -type l \) -printf '%P\n';
             cd "$system/$root" 2>/dev/null && find . \( -type f -o -type l \) -printf '%P\n') | sort -u
        )
    done
    for rel in kernel initrd systemd; do
        if [[ "$(readlink "$current/$rel" 2>/dev/null)" != "$(readlink "$system/$rel" 2>/dev/null)" ]]; then
            scope_changed_other+=("$rel")
        fi
    done
}
only_target_changed=0
guard_only_switch() {
    # Fail closed: an --only switch may proceed only when the target
    # account's home-manager unit is the sole difference in the surface
    # classified by detect_switch_scope. Everything else needs a full
    # rebuild, whose state-unit and Backrest handling stays authoritative.
    local system="$1"
    local new_units="$system/etc/systemd/system"
    local target="" file changed_other=()
    only_target_changed=0
    for file in "$new_units"/home-manager-*.service; do
        [[ -e "$file" ]] || continue
        case "$(basename "$file")" in
            *"$only".service) target="$file" ;;
        esac
    done
    if [[ -z "$target" ]]; then
        printf 'rebuild: --only %s: no home-manager account matches this principal\n' "$only" >&2
        return 1
    fi
    detect_switch_scope "$system"
    cmp -s "/run/current-system/etc/systemd/system/$(basename "$target")" "$target" || only_target_changed=1
    changed_other=("${scope_changed_other[@]}")
    for file in "${scope_changed_hm[@]}"; do
        [[ "$file" == "$(basename "$target")" ]] || changed_other+=("etc/systemd/system/$file")
    done
    if (( ${#changed_other[@]} )); then
        printf 'rebuild: --only %s refused; changes outside the target account require a full rebuild:\n' "$only" >&2
        printf '  %s\n' "${changed_other[@]:0:10}" >&2
        (( ${#changed_other[@]} > 10 )) && printf '  ... and %d more\n' "$(( ${#changed_other[@]} - 10 ))" >&2
        return 1
    fi
    return 0
}
run_switch() {
    # Activate the toplevel built above directly. `nixos-rebuild switch`
    # would evaluate the flake a second time (tens of seconds) only to build
    # this same output, then do exactly these two steps: register the system
    # profile generation, then run switch-to-configuration.
    local system="$1" status=0
    sudo nix-env --profile /nix/var/nix/profiles/system --set "$system" || status=$?
    if (( ! status )); then
        sudo "$system/bin/switch-to-configuration" switch || status=$?
    fi
    restore_backrest || (( status )) || status=1
    return "$status"
}
switch_nixos() {
    local flake="path:$state_dir/deployment" system unit changed=0 status=0
    # Build first, without privileges: it can outlast sudo's credential cache.
    system="$(nix build --no-link --print-out-paths --no-write-lock-file \
        "$flake#nixosConfigurations.$hostname.config.system.build.toplevel")"
    # Invalidate, then validate: combined `sudo -k -v` prompts but discards
    # the credential, so the next sudo command would prompt a second time.
    sudo -k
    sudo -v
    if [[ -n "$only" ]]; then
        guard_only_switch "$system" || return 1
        if (( ! only_target_changed )); then
            printf 'rebuild: --only %s: account unchanged; nothing to switch\n' "$only" >&2
            return 0
        fi
        # Backrest's own account also holds state paths its container adopts.
        if [[ "$only" == backrest ]] && [[ "$(backrest_ctl is-active backrest.service || true)" == active ]]; then
            printf 'rebuild: --only backrest; stopping Backrest for the switch (a running backup is interrupted)\n' >&2
            backrest_stopped=1
            trap restore_backrest EXIT
            backrest_ctl stop backrest.service
        fi
        run_switch "$system"
        return $?
    fi
    # Automatic scoping: when the built system differs from the running one
    # in exactly one account's home-manager unit and nothing else in the
    # guarded surface, only that account can restart. Say so, and skip the
    # state-unit Backrest handling (those units are unchanged by proof).
    detect_switch_scope "$system"
    if (( ${#scope_changed_other[@]} == 0 && ${#scope_changed_hm[@]} == 1 )); then
        local scoped_name
        scoped_name="$(basename "${scope_changed_hm[0]}" .service)"
        scoped_name="${scoped_name#home-manager-}"
        scoped_name="${scoped_name//\\x2d/-}"
        printf 'rebuild: only %s changed; scoped switch (other accounts untouched)\n' "$scoped_name" >&2
        if [[ "$scoped_name" == homelab-backrest ]] && [[ "$(backrest_ctl is-active backrest.service || true)" == active ]]; then
            printf 'rebuild: stopping Backrest for the scoped switch (a running backup is interrupted)\n' >&2
            backrest_stopped=1
            trap restore_backrest EXIT
            backrest_ctl stop backrest.service
        fi
        run_switch "$system"
        return $?
    fi
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
    run_switch "$system"
    return $?
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
