#!/bin/bash
# Stop GOST service and optionally stop the Lima VM

set -e

VM_NAME="gost"

echo "=== Stopping GOST Service ==="

# Check if VM exists
if ! limactl list | grep -q "^$VM_NAME"; then
    echo "VM '$VM_NAME' does not exist"
    exit 0
fi

# Stop GOST service
echo "Stopping GOST service..."
limactl shell "$VM_NAME" systemctl --user stop gost.service || true

echo "✓ GOST service stopped"

# Optionally stop VM (pass --stop-vm flag)
if [ "$1" = "--stop-vm" ]; then
    echo "Stopping Lima VM..."
    limactl stop "$VM_NAME"
    echo "✓ VM stopped"
else
    echo "  VM still running"
    echo "  To stop VM: $0 --stop-vm"
fi
