#!/bin/bash
# Start GOST Lima VM and client as a background service

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_NAME="gost"
GOST_DEV_DIR="${GOST_DEV_DIR:-"$HOME/Developer/gost-proxy"}"
GOST_ENV_FILE="$GOST_DEV_DIR/client/.env"

if [ ! -f "$GOST_ENV_FILE" ]; then
    echo "ERROR: $GOST_ENV_FILE not found"
    echo "Copy .env.example and fill in your credentials and server address"
    exit 1
fi

# shellcheck disable=SC1090
source "$GOST_ENV_FILE"

: "${CF_ACCESS_CLIENT_ID:?Missing CF_ACCESS_CLIENT_ID in $GOST_ENV_FILE}"
: "${CF_ACCESS_CLIENT_SECRET:?Missing CF_ACCESS_CLIENT_SECRET in $GOST_ENV_FILE}"
: "${GOST_SERVER_ADDRESS:?Missing GOST_SERVER_ADDRESS in $GOST_ENV_FILE}"

GOST_SERVER_HOSTNAME="${GOST_SERVER_HOSTNAME:-${GOST_SERVER_ADDRESS%%:*}}"
GOST_CA_SERVER="${GOST_CA_SERVER:-$GOST_SERVER_ADDRESS}"

echo "=== Starting GOST Service ==="

# Check if VM exists, create if not
if ! limactl list 2>/dev/null | grep -q "^$VM_NAME"; then
    echo "Creating Lima VM '$VM_NAME'..."
    limactl start --name="$VM_NAME" "$SCRIPT_DIR/lima-gost.yaml"
else
    # Check if VM is running
    VM_STATUS=$(limactl list "$VM_NAME" 2>/dev/null | tail -n +2 | awk '{print $2}')
    if [ "$VM_STATUS" != "Running" ]; then
        echo "Starting Lima VM '$VM_NAME'..."
        limactl start "$VM_NAME"
    else
        echo "Lima VM '$VM_NAME' already running"
    fi
fi

# Wait for VM to be ready
echo "Waiting for VM to be ready..."
sleep 3

# Extract and install the TLS inspection CA dynamically (macOS only).
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "Extracting proxy CA certificate..."
    if echo "" | openssl s_client -showcerts -connect "$GOST_CA_SERVER" 2>/dev/null | \
       awk "/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/" > /tmp/gost-ca-chain.crt 2>/dev/null; then

        # Extract just the root CA (last certificate in chain)
        tail -32 /tmp/gost-ca-chain.crt > /tmp/gost-ca-root.crt

        # Copy to VM and install
        limactl copy /tmp/gost-ca-root.crt "$VM_NAME:/tmp/gost-ca-root.crt"
        limactl shell "$VM_NAME" sudo bash -c "cp /tmp/gost-ca-root.crt /usr/local/share/ca-certificates/ && update-ca-certificates" 2>/dev/null || true

        # Clean up
        rm -f /tmp/gost-ca-chain.crt /tmp/gost-ca-root.crt

        echo "✓ Proxy CA installed"
    else
        echo "⚠ Could not extract proxy CA (may already be installed or not needed)"
    fi
fi

# Install Go if not present in VM
if ! limactl shell "$VM_NAME" bash -c "command -v go" &>/dev/null; then
    echo "Installing Go in VM..."

    # Get latest Go version dynamically
    LATEST_GO=$(curl -sL 'https://go.dev/VERSION?m=text' 2>/dev/null | head -n1 | tr -d '\n')
    GO_VERSION=${LATEST_GO#go}

    # Fallback to specific version if API fails
    if [ -z "$GO_VERSION" ]; then
        GO_VERSION="1.23.5"
    fi

    GO_ARCH="arm64"

    echo "Downloading Go ${GO_VERSION}..."

    # Create install script
    cat > /tmp/install-go.sh << 'EOF'
#!/bin/bash
set -e
if [ ! -d '/usr/local/go' ]; then
    cd /tmp
    echo "Fetching Go tarball..."
    wget --progress=dot:giga "https://go.dev/dl/go${GO_VERSION}.linux-${GO_ARCH}.tar.gz"
    echo "Extracting Go..."
    sudo tar -C /usr/local -xzf "go${GO_VERSION}.linux-${GO_ARCH}.tar.gz"
    rm "go${GO_VERSION}.linux-${GO_ARCH}.tar.gz"

    # Add to PATH
    if ! grep -q '/usr/local/go/bin' /etc/profile; then
        echo 'export PATH=$PATH:/usr/local/go/bin' | sudo tee -a /etc/profile >/dev/null
    fi
    echo "Verifying installation..."
fi
/usr/local/go/bin/go version
EOF

    # Substitute variables in the script
    sed -i "s/\${GO_VERSION}/${GO_VERSION}/g" /tmp/install-go.sh
    sed -i "s/\${GO_ARCH}/${GO_ARCH}/g" /tmp/install-go.sh

    # Copy and run in VM
    limactl copy /tmp/install-go.sh "$VM_NAME:/tmp/"
    limactl shell "$VM_NAME" bash /tmp/install-go.sh
    rm -f /tmp/install-go.sh

    echo "✓ Go ${GO_VERSION} installed"
else
    GO_VERSION=$(limactl shell "$VM_NAME" /usr/local/go/bin/go version 2>/dev/null | awk '{print $3}')
    echo "✓ Go already installed (${GO_VERSION})"
fi

# Check if GOST is installed in VM
if ! limactl shell "$VM_NAME" bash -c "command -v gost" &>/dev/null; then
    echo "Building GOST in VM..."

    # Create build directory and copy gost-x source from Developer folder
    limactl shell "$VM_NAME" bash -c "mkdir -p /tmp/gost-build/gost-x"

    # Copy gost-x directory using tar for efficiency
    echo "Copying GOST source code from $GOST_DEV_DIR..."
    tar -czf /tmp/gost-x.tar.gz -C "$GOST_DEV_DIR" gost-x 2>&1 | grep -v "Ignoring unknown extended header" || true
    limactl copy /tmp/gost-x.tar.gz "$VM_NAME:/tmp/"
    limactl shell "$VM_NAME" bash -c "cd /tmp/gost-build && tar -xzf /tmp/gost-x.tar.gz && rm /tmp/gost-x.tar.gz"
    rm -f /tmp/gost-x.tar.gz

    # Copy build script
    limactl copy "$SCRIPT_DIR/build-gost-in-vm.sh" "$VM_NAME:/tmp/"

    # Build GOST
    echo "Building GOST binary (this may take a minute)..."
    limactl shell "$VM_NAME" bash /tmp/build-gost-in-vm.sh

    # Install to /usr/local/bin
    limactl shell "$VM_NAME" sudo cp /tmp/gost-build/gost/gost /usr/local/bin/gost
    limactl shell "$VM_NAME" sudo chmod +x /usr/local/bin/gost

    echo "✓ GOST built and installed"
fi

# Prepare config with environment variables
echo "Preparing GOST config..."

# Create config with substituted values using centralized config
export CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET GOST_SERVER_ADDRESS GOST_SERVER_HOSTNAME
envsubst < "$GOST_DEV_DIR/client/config.yaml" > /tmp/gost-config-subst.yaml

# Copy config to VM
limactl copy /tmp/gost-config-subst.yaml "$VM_NAME:/tmp/gost-config.yaml"
rm -f /tmp/gost-config-subst.yaml

# Install systemd service if not exists
if ! limactl shell "$VM_NAME" systemctl is-enabled gost.service &>/dev/null; then
    echo "Installing systemd service..."

    # Copy service file
    limactl copy /tmp/gost.service "$VM_NAME:/tmp/"
    limactl shell "$VM_NAME" sudo cp /tmp/gost.service /etc/systemd/system/

    # Reload systemd and enable service
    limactl shell "$VM_NAME" sudo systemctl daemon-reload
    limactl shell "$VM_NAME" sudo systemctl enable gost.service

    echo "✓ Systemd service installed and enabled"
fi

# Restart GOST service
echo "Starting GOST service..."
limactl shell "$VM_NAME" sudo systemctl restart gost.service

# Wait for service to start
sleep 2

# Check if GOST is running
if limactl shell "$VM_NAME" systemctl is-active gost.service &>/dev/null; then
    echo "✓ GOST service started successfully"
    echo "✓ System service is running"
    echo "✓ SOCKS5 proxy available at localhost:1080"
    echo ""
    echo "Commands:"
    echo "  Status:     limactl shell $VM_NAME sudo systemctl status gost.service"
    echo "  Logs:       limactl shell $VM_NAME sudo journalctl -u gost.service -f"
    echo "  Stop:       limactl shell $VM_NAME sudo systemctl stop gost.service"
    echo "  Restart:    limactl shell $VM_NAME sudo systemctl restart gost.service"
    echo "  Disable:    limactl shell $VM_NAME sudo systemctl disable gost.service"
    echo "  Test:       curl --socks5-hostname localhost:1080 https://ifconfig.me"
else
    echo "✗ Failed to start GOST service"
    echo "Check status: limactl shell $VM_NAME sudo systemctl status gost.service"
    echo "Check logs:   limactl shell $VM_NAME sudo journalctl -u gost.service -n 50"
    exit 1
fi
