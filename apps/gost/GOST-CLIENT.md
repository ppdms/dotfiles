# GOST client notes

Start or rebuild:

```bash
./start-gost-service.sh
```

Check the service:

```bash
limactl shell gost sudo systemctl status gost.service
limactl shell gost sudo journalctl -u gost.service -f
```

Restart it:

```bash
limactl shell gost sudo systemctl restart gost.service
```

Test the proxy:

```bash
curl --socks5-hostname localhost:1080 https://ifconfig.me
```

Stop it:

```bash
./stop-gost-service.sh
```

Useful environment variables:

```text
GOST_DEV_DIR
GOST_SERVER_ADDRESS
GOST_SERVER_HOSTNAME
GOST_CA_SERVER
CF_ACCESS_CLIENT_ID
CF_ACCESS_CLIENT_SECRET
```

The start script creates the Lima VM, installs the proxy CA, builds the custom
GOST source, writes the VM config, and starts `gost.service`.
