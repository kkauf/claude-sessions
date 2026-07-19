#!/bin/bash
# Build SessionPicker.app — no Xcode project, one swiftc invocation.
# Usage: ./build-app.sh [--install]   (--install copies to ~/Applications and launches)
set -euo pipefail
cd "$(dirname "$0")"

APP=SessionPicker.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

swiftc -O -swift-version 5 -o "$APP/Contents/MacOS/SessionPicker" SessionPicker.swift

# RepoPath tells the app where the CLI + indexer live (this clone), so any
# clone location works without configuration.
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>earth.kaufmann.SessionPicker</string>
    <key>CFBundleName</key><string>SessionPicker</string>
    <key>CFBundleExecutable</key><string>SessionPicker</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>RepoPath</key><string>$PWD</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

codesign --force --sign - "$APP" 2>/dev/null || true
echo "Built $APP"

if [[ "${1:-}" == "--install" ]]; then
  mkdir -p ~/Applications
  rm -rf ~/Applications/"$APP"
  cp -R "$APP" ~/Applications/
  open ~/Applications/"$APP"
  echo "Installed and launched ~/Applications/$APP"
  echo "Add it as a Login Item: System Settings → General → Login Items"
fi
