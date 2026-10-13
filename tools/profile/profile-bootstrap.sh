# Shared Bash bootstrap for the packaged profile helper.
profile_bootstrap_dir="$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

profile_git_source_url() {
    local checkout root url character escaped i LC_ALL=C
    checkout="$(CDPATH= cd -P -- "$1" && pwd -P)" || {
        printf '%s\n' 'profile: Missing public Git checkout' >&2
        return 1
    }
    root="$(git -C "$checkout" rev-parse --show-toplevel 2>/dev/null)" || {
        printf '%s\n' 'profile: Bootstrap requires a public Git checkout with a committed HEAD' >&2
        return 1
    }
    if [[ "$root" != "$checkout" ]] ||
        ! git -C "$checkout" rev-parse --verify 'HEAD^{commit}' >/dev/null 2>&1; then
        printf '%s\n' 'profile: Bootstrap requires a public Git checkout with a committed HEAD' >&2
        return 1
    fi
    url="git+file://"
    for ((i = 0; i < ${#checkout}; i++)); do
        character="${checkout:i:1}"
        case "$character" in
            [a-zA-Z0-9/._~-]) url+="$character" ;;
            *) printf -v escaped '%%%02X' "'$character"; url+="$escaped" ;;
        esac
    done
    printf '%s\n' "$url"
}

profile_exec() {
    local profile_bin source_url
    profile_bin="$(type -P profile || true)"
    if [[ -n "$profile_bin" ]]; then
        exec "$profile_bin" "$@"
    fi
    source_url="$(profile_git_source_url "$profile_bootstrap_dir/../..")" || return
    exec nix run --no-write-lock-file "$source_url#profile" -- "$@"
}

profile_run() (
    profile_exec "$@"
)
