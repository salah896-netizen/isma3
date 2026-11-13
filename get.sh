#!/bin/bash
# Downloads (or updates) Isma3 into ~/Isma3, then opens the installer.
# Usage:  curl -fsSL https://raw.githubusercontent.com/salah896-netizen/isma3/main/get.sh | bash
set -e
DEST="$HOME/Isma3"
TMP="$(mktemp -d)"
echo "Downloading Isma3..."
curl -fsSL https://github.com/salah896-netizen/isma3/archive/refs/heads/main.zip -o "$TMP/isma3.zip"
unzip -q "$TMP/isma3.zip" -d "$TMP"
mkdir -p "$DEST"
# copy the app, but never touch your settings, recordings, models or Python environment
rsync -a --exclude docs --exclude config.json --exclude data --exclude models --exclude .venv "$TMP/isma3-main/" "$DEST/"
chmod +x "$DEST"/*.command "$DEST"/*.sh "$DEST"/assets/*.sh
rm -rf "$TMP"
echo "Isma3 is in $DEST. Opening the installer..."
open "$DEST/install.command"
