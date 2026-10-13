#!/usr/bin/env bash
# Scan tracked working-tree files for known credentials, ciphertext,
# private identifiers and local artifacts. Report filenames, not payloads.
# Exit 1 on findings, scan errors or untracked files.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1
self="tools/check-secrets.sh"
fail=0

finding() { printf 'FAIL  %s\n' "$1"; fail=1; }

# scan PATTERN LABEL [extra git-grep pathspec exclusions...]
scan() {
    local pattern="$1" label="$2"; shift 2
    local hits status
    if hits="$(git grep -l -a -E -e "$pattern" -- . "$@" 2>&1)"; then
        finding "$label"
        printf '%s\n' "$hits"
    else
        status=$?
        (( status == 1 )) || finding "scan failed: $label"
    fi
}

# Real key material: a header followed by a long base64 body. (Several
# files contain the bare words "PRIVATE KEY" because they *detect* it;
# those do not match this.)
scan 'BEGIN [A-Z ]*PRIVATE KEY-----[[:space:]]*$' "possible private key header"
git grep -n -I -A1 -E 'BEGIN [A-Z ]*PRIVATE KEY-----' -- . ":(exclude)$self" 2>/dev/null \
    | grep -E '^[^-]+-[A-Za-z0-9+/=]{40,}' >/dev/null \
    && finding "private key body found"

scan 'AGE-SECRET-KEY-1[0-9A-Z]{58}' "possible age secret key"
scan '(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}' "possible GitHub token"
scan 'sk-(ant-)?[A-Za-z0-9_-]{30,}|AKIA[0-9A-Z]{16}|xox[bap]-[A-Za-z0-9-]{10,}' "possible API token"
# Ciphertext is excluded from public source, including examples. Detector
# expressions in source code do not match these complete payload/header forms.
scan 'ENC\[AES256_GCM,data:[A-Za-z0-9+/=]+,iv:[A-Za-z0-9+/=]+,tag:[A-Za-z0-9+/=]+,type:[a-z]+\]' "SOPS ciphertext"
scan 'BEGIN (AGE ENCRYPTED FILE|PGP MESSAGE|ENCRYPTED PRIVATE KEY)-----[[:space:]]*$|^age-encryption\.org/v1' "encrypted payload"
encrypted_paths="$(git ls-files | grep -E '(^|/)[^/]*\.sops\.[^/]+$|\.(age|cred|enc|gpg|pgp)$' || true)"
if [[ -n "$encrypted_paths" ]]; then
    finding "tracked ciphertext filename"
    printf '%s\n' "$encrypted_paths"
fi

# Identifier patterns are excluded from matching their own detector.
scan 'ppdms' "personal domain fragment" ":(exclude)$self"
scan 'papadim|vassilis|vasileios' "personal name" ":(exclude)$self"
hits="$(git grep -n -I -E '/Users/[a-z]+/' -- . ":(exclude)$self" 2>/dev/null \
    | grep -v '/Users/example/' | cut -d: -f1 | sort -u || true)"
if [[ -n "$hits" ]]; then
    finding "personal home path"
    printf '%s\n' "$hits"
fi
# The upstream Kitty theme contains author email addresses.
scan '[A-Za-z0-9._%+-]+@(gmail\.com|protonmail\.com|yahoo\.|hotmail\.|outlook\.com)' "personal email address" \
    ':(exclude)programs/kitty/Gruvbox_Dark_Hard.conf'

# Tracked generated and local artifacts are rejected.
if git ls-files | grep -qE '__pycache__|\.py[cd]$|\.DS_Store$|config\.yml\.lock$'; then
    finding "tracked build/local droppings:"
    git ls-files | grep -E '__pycache__|\.py[cd]$|\.DS_Store$|config\.yml\.lock$'
fi

# Example profiles use reserved example domains.
if git grep -n -I -E '[a-z0-9-]+\.(com|net|org|gr|io|dev)\b' -- 'examples/*/profile.json' 2>/dev/null \
     | grep -v 'example' >/dev/null; then
    finding "non-example domain in an example profile"
    git grep -l -I -E '[a-z0-9-]+\.(com|net|org|gr|io|dev)\b' -- 'examples/*/profile.json'
fi

if [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
    finding "untracked files are not covered by this scan:"
    git ls-files --others --exclude-standard
fi

printf '\n'
if (( fail )); then
    printf 'Secret check failed.\n'
    exit 1
fi
printf 'No automated findings.\n'
