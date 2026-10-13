#!/usr/bin/env bash
set -euo pipefail
script_dir="$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$script_dir/profile-bootstrap.sh"
profile_exec render "$@"
