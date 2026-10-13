#!/bin/bash
set -u

PORT_FILE=/etc/openvpn/custom/forwarded_port
daemon_pid=
sleep_pid=

log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

stop() {
    trap - TERM INT
    if [[ -n ${sleep_pid:-} ]]; then
        kill "$sleep_pid" 2>/dev/null || true
    fi
    if [[ -n ${daemon_pid:-} ]]; then
        kill -TERM "$daemon_pid" 2>/dev/null || true
        wait "$daemon_pid" 2>/dev/null || true
    fi
    exit 0
}

/usr/bin/transmission-daemon -g /config -f &
daemon_pid=$!
trap stop TERM INT

paused=false
last_port=
while kill -0 "$daemon_pid" 2>/dev/null; do
    if [[ $paused == false ]] && /usr/bin/transmission-remote 127.0.0.1:9091 -l >/dev/null 2>&1; then
        if /usr/bin/transmission-remote 127.0.0.1:9091 -t all --stop >/dev/null 2>&1; then
            log 'all torrents stopped at container start'
            paused=true
        fi
    fi

    port=$(tr -cd '0-9' <"$PORT_FILE" 2>/dev/null || true)
    if [[ $port =~ ^[0-9]{1,5}$ ]] && (( port > 0 && port <= 65535 )) && [[ $port != "$last_port" ]]; then
        if /usr/bin/transmission-remote 127.0.0.1:9091 -p "$port" >/dev/null 2>&1; then
            log "peer port set to $port"
            last_port=$port
        else
            log "failed to set peer port $port"
        fi
    fi

    sleep 5 &
    sleep_pid=$!
    wait "$sleep_pid" 2>/dev/null || true
    sleep_pid=
done

wait "$daemon_pid"
