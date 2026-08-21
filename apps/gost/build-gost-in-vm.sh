#!/bin/bash
# Build GOST inside Lima VM (run this inside the VM)

set -e

echo "=== Building GOST in VM ==="

# Clone main GOST repo if not exists
if [ ! -d "/tmp/gost-build/gost" ]; then
    echo "Cloning GOST repository..."
    cd /tmp/gost-build
    git clone --depth 1 https://github.com/go-gost/gost.git
fi

cd /tmp/gost-build/gost

# Update dependencies
echo "Updating go.mod to use local gost-x..."
export PATH=$PATH:/usr/local/go/bin
export GOPATH=$HOME/go

# Replace gost-x dependency with our local modified version
go mod edit -replace github.com/go-gost/x=/tmp/gost-build/gost-x

# Tidy dependencies
echo "Running go mod tidy..."
go mod tidy

# Build
echo "Building GOST..."
go build -v -o gost ./cmd/gost

# Verify
if [ -f "gost" ]; then
    ls -lh gost
    /usr/local/go/bin/go version
    echo "✓ GOST built successfully"
else
    echo "✗ Build failed - gost binary not found"
    exit 1
fi
