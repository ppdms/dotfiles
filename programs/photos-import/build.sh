#!/usr/bin/env bash
# Build the same immutable, signed application used by Home Manager.

set -euo pipefail

script_dir="$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$script_dir/../../tools/profile/profile-bootstrap.sh"
source_url="$(profile_git_source_url "$script_dir/../..")"
output="$(nix build --no-link --print-out-paths --no-write-lock-file "$source_url#photos-import")"

printf 'Application: %s/Applications/photos-import.app\n' "$output"
printf 'CLI: %s/bin/photos-import /path/to/image.png\n' "$output"
