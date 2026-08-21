#!/bin/bash
# Uninstall GOST LaunchAgent

set -e

PLIST_NAME="com.gost.client"
PLIST_FILE="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo "=== Uninstalling GOST LaunchAgent ==="

if [ -f "$PLIST_FILE" ]; then
    echo "Unloading LaunchAgent..."
    launchctl unload "$PLIST_FILE" 2>/dev/null || true

    echo "Removing plist file..."
    rm "$PLIST_FILE"

    echo "✓ GOST LaunchAgent uninstalled"
else
    echo "LaunchAgent not installed"
fi

echo ""
echo "To stop the VM:"
echo "  ./stop-gost-service.sh --stop-vm"
echo ""
echo "To delete the VM completely:"
echo "  limactl delete gost"
