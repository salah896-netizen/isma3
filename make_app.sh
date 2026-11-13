#!/bin/bash
# Builds ~/Applications/Isma3.app, a Dock/Spotlight launcher for this Isma3 folder.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Isma3.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$DIR/assets/Isma3.icns" "$APP/Contents/Resources/Isma3.icns"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Isma3</string>
  <key>CFBundleDisplayName</key><string>Isma3</string>
  <key>CFBundleIdentifier</key><string>io.github.isma3.launcher</string>
  <key>CFBundleExecutable</key><string>Isma3</string>
  <key>CFBundleIconFile</key><string>Isma3</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict></plist>
PLIST
sed "s#__ISMA3_DIR__#$DIR#" "$DIR/assets/launcher.sh" > "$APP/Contents/MacOS/Isma3"
chmod +x "$APP/Contents/MacOS/Isma3"
touch "$APP"
echo "Created $APP. Drag it to your Dock to keep it there."
