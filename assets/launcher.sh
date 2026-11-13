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
# restart a running copy if the app was updated since it started
if [ -f data/server.pid ] && curl -s "$URL/api/health" >/dev/null; then
  if [ app.py -nt data/server.pid ] || [ pipeline.py -nt data/server.pid ]; then
    kill "$(cat data/server.pid)" 2>/dev/null; sleep 1.5
  fi
elif curl -s "$URL/api/health" >/dev/null; then
  pkill -f ".venv/bin/python app.py"; pkill -f "$DIR/app.py"; sleep 1.5   # an older copy without a pid file
fi
if ! curl -s "$URL/api/health" >/dev/null; then
  curl -s http://127.0.0.1:11434/api/tags >/dev/null || { open -ga Ollama; sleep 4; }
  mkdir -p data
  # start the engine in its own session, so it keeps running after this launcher closes
  # (macOS shuts down everything an app started when the app quits)
  .venv/bin/python - <<'PY'
import os, subprocess
log = open("data/isma3.log", "ab")
subprocess.Popen([".venv/bin/python", "app.py"], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                 start_new_session=True, close_fds=True, env=os.environ)
PY
  for i in $(seq 1 40); do curl -s "$URL/api/health" >/dev/null && break; sleep 0.5; done
fi
if [ -d "/Applications/Google Chrome.app" ]; then
  open -na "Google Chrome" --args --app="$URL"   # its own window, no tabs
else
  open "$URL"
fi
