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
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

# Bake the icon into the bundle from the same drawing code as the runtime
# Dock icon — a pinned Dock tile only shows the logo after quit if the .icns
# exists on disk.
ICON_TMP=$(mktemp -d)
"$APP/Contents/MacOS/SessionPicker" --dump-iconset "$ICON_TMP/AppIcon.iconset"
mkdir -p "$APP/Contents/Resources"
iconutil -c icns "$ICON_TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$ICON_TMP"
[[ -s "$APP/Contents/Resources/AppIcon.icns" ]] || { echo "AppIcon.icns missing" >&2; exit 1; }

codesign --force --sign - "$APP" 2>/dev/null || true
echo "Built $APP"

if [[ "${1:-}" == "--install" ]]; then
  # Update the installed bundle IN PLACE (no rm -rf): replacing the directory
  # would invalidate the Dock pin's bookmark — the pinned tile then loses its
  # icon or dies entirely on the next quit.
  mkdir -p ~/Applications/"$APP"
  rsync -a --delete "$APP"/ ~/Applications/"$APP"/
  open ~/Applications/"$APP"
  echo "Installed and launched ~/Applications/$APP"
  echo "Add it as a Login Item: System Settings → General → Login Items"
fi
