# GOST proxy

Custom GOST client running as a systemd service inside a Lima VM.

Source and credentials live under:

```text
$GOST_DEV_DIR
```

Default source directory:

```text
$HOME/Developer/gost-proxy
```

Required `client/.env` values:

```bash
CF_ACCESS_CLIENT_ID=...
CF_ACCESS_CLIENT_SECRET=...
GOST_SERVER_ADDRESS=host:443
```

Commands:

```bash
./start-gost-service.sh
./stop-gost-service.sh
./test-auth.sh
```

SOCKS proxy:

```text
localhost:1080
```
