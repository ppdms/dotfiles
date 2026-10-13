#!/usr/bin/env bash
set -euo pipefail
script_dir="$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$script_dir/profile-bootstrap.sh"
# The converter uses the helper and schema from this checkout.
source_url="$(profile_git_source_url "$script_dir/../..")"
exec nix run --no-write-lock-file "$source_url#profile" -- python "$script_dir/convert-profile.py" "$@"
