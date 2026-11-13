#!/bin/bash
# Isma3 launcher: starts the app quietly if it isn't running, then opens its window.
DIR="__ISMA3_DIR__"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export HF_HUB_OFFLINE=1
cd "$DIR" || { osascript -e 'display alert "Isma3 folder not found" message "Expected it at '"$DIR"'."'; exit 1; }
PORT=$(.venv/bin/python -c "import json,os;c={};[c.update(json.load(open(f))) for f in ('config.example.json','config.json') if os.path.exists(f)];print(c.get('port',8765))" 2>/dev/null || echo 8765)
URL="http://localhost:$PORT"
# if the icon is clicked twice quickly, let the first click do the starting
if ! mkdir /tmp/isma3-launch.lock 2>/dev/null; then
  sleep 3; open "$URL"; exit 0
fi
trap 'rmdir /tmp/isma3-launch.lock 2>/dev/null' EXIT
# The engine runs as a macOS background service (a LaunchAgent), so it keeps running after this
# launcher closes. macOS stops anything an app starts directly as soon as the app quits.
LABEL="io.github.isma3.engine"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
mkdir -p data "$HOME/Library/LaunchAgents"
TMP_PLIST="$(mktemp)"
cat > "$TMP_PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/.venv/bin/python</string><string>$DIR/app.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HF_HUB_OFFLINE</key><string>1</string>
  </dict>
  <key>StandardOutPath</key><string>$DIR/data/isma3.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/isma3.log</string>
  <key>RunAtLoad</key><false/>
  <key>KeepAlive</key><false/>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
PL
if ! cmp -s "$TMP_PLIST" "$PLIST"; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
  cp "$TMP_PLIST" "$PLIST"
fi
rm -f "$TMP_PLIST"
launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null

if curl -s "$URL/api/health" >/dev/null; then
  if [ ! -f data/server.pid ] || [ app.py -nt data/server.pid ] || [ pipeline.py -nt data/server.pid ] || [ live.py -nt data/server.pid ]; then
    # updated since it started (or an older copy started another way): stop it so the new code loads
    [ -f data/server.pid ] && kill "$(cat data/server.pid)" 2>/dev/null
    pkill -f ".venv/bin/python app.py"; pkill -f "$DIR/app.py"
    for i in $(seq 1 20); do curl -s "$URL/api/health" >/dev/null || break; sleep 0.25; done
  fi
fi
if ! curl -s "$URL/api/health" >/dev/null; then
  curl -s http://127.0.0.1:11434/api/tags >/dev/null || { open -ga Ollama; sleep 4; }
  if ! launchctl kickstart "$DOMAIN/$LABEL" 2>/dev/null; then
    # fallback if the background service can't be used
    .venv/bin/python - <<'PY'
import os, subprocess
log = open("data/isma3.log", "ab")
subprocess.Popen([".venv/bin/python", "app.py"], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                 start_new_session=True, close_fds=True, env=os.environ)
PY
  fi
  for i in $(seq 1 40); do curl -s "$URL/api/health" >/dev/null && break; sleep 0.5; done
fi
if [ -d "/Applications/Google Chrome.app" ]; then
  open -na "Google Chrome" --args --app="$URL"   # its own window, no tabs
else
  open "$URL"
fi
