#!/bin/bash
# Install GOST client as macOS LaunchAgent for automatic startup

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.gost.client"
PLIST_FILE="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo "=== Installing GOST LaunchAgent ==="

# Check if .env exists
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "ERROR: .env file not found"
    echo "Please create .env from .env.example first"
    exit 1
fi

# Create LaunchAgents directory if it doesn't exist
mkdir -p "$HOME/Library/LaunchAgents"

# Create plist file
cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>

    <key>ProgramArguments</key>
    <array>
        <string>$SCRIPT_DIR/start-gost-service.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>/tmp/gost-launchagent.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/gost-launchagent.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

# Make scripts executable
chmod +x "$SCRIPT_DIR/start-gost-service.sh"
chmod +x "$SCRIPT_DIR/stop-gost-service.sh"

# Load the LaunchAgent
echo "Loading LaunchAgent..."
launchctl unload "$PLIST_FILE" 2>/dev/null || true
launchctl load "$PLIST_FILE"

echo "✓ GOST LaunchAgent installed and loaded"
echo ""
echo "Service will start automatically at login."
echo ""
echo "Commands:"
echo "  View logs:     tail -f /tmp/gost-launchagent.log"
echo "  GOST logs:     limactl shell gost tail -f /tmp/gost.log"
echo "  Restart:       launchctl kickstart -k gui/\$(id -u)/$PLIST_NAME"
echo "  Stop:          launchctl stop $PLIST_NAME"
echo "  Uninstall:     launchctl unload $PLIST_FILE && rm $PLIST_FILE"
echo "  Test proxy:    curl --socks5-hostname localhost:1080 https://ifconfig.me"
