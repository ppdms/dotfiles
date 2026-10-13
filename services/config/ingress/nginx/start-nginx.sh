#!/bin/sh
set -eu
umask 077

# Stale-socket cleanup: an unclean exit leaves the bound pathname behind and
# the next start fails with EADDRINUSE. Unlinking is safe (existing dials
# ride the unlinked inode); cloudflared redials the fresh path. nginx itself
# binds this socket (cloudflared only dials it), so always load the tunnel
# vhosts without an existence check on the unlinked path.
rm -f /run/cloudflared-origin/http.sock

# Tunnel vhosts share one cloudflared-origin Unix socket. Stage them via a
# tmpfs symlink: keeps the read-only ingress tree untouched while letting
# `nginx -t` see the same files the live include loads.
ln -sfn /etc/nginx/ingress/sites-tunnel /tmp/nginx-sites-tunnel

# All upstreams are registry loopback endpoints; container DNS is unnecessary.
# nginx can chmod Unix listeners independently of umask. The host directory
# is setgid nginx:homelab-origin and non-world-traversable; explicitly restrict
# its socket before cloudflared connects through its read-only bind mount.
/usr/sbin/nginx -g 'daemon off;' &
nginx_pid=$!
stop_nginx() {
    trap '' TERM INT
    kill -TERM "$nginx_pid" 2>/dev/null || true
    wait "$nginx_pid" || true
    exit 0
}
trap stop_nginx TERM INT
while [ ! -S /run/cloudflared-origin/http.sock ]; do
    if ! kill -0 "$nginx_pid" 2>/dev/null; then
        wait "$nginx_pid"
        exit 1
    fi
    sleep 0.05
done
if ! chmod 0660 /run/cloudflared-origin/http.sock; then
    kill -TERM "$nginx_pid" 2>/dev/null || true
    wait "$nginx_pid" || true
    exit 1
fi
wait "$nginx_pid"
