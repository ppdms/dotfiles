#!/usr/bin/env bash
# Builds and codesigns photos-import.app
# Source: public/bin/photos-import.swift
# Info.plist source: public/bin/photos-import-app/Info.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$SCRIPT_DIR/photos-import.app"

echo "Building $APP..."

mkdir -p "$APP/Contents/MacOS"
cp "$SCRIPT_DIR/photos-import-app/Info.plist" "$APP/Contents/"
swiftc "$SCRIPT_DIR/photos-import.swift" -o "$APP/Contents/MacOS/photos-import"
codesign --sign - "$APP"

echo "To test:"
echo "  open -W -n '$APP' --args /path/to/image.png"
