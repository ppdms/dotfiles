#!/bin/bash
# Bypass s6-overlay: its preinit rejects rootless /run ownership
# (uid 0 instead of 1000), which cannot be changed without privileges.
# Like Transmission's nonroot entrypoint, run Kavita directly:
# the single svc-kavita line, minus the s6 supervision wrappers.
set -u

export LD_PRELOAD="$(find / -name libjemalloc.so.2 | head -1)"
cd /app/kavita
exec /app/kavita/Kavita
