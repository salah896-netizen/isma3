#!/bin/bash
# Isma3 launcher: starts the app quietly if it isn't running, then opens its window.
DIR="__ISMA3_DIR__"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export HF_HUB_OFFLINE=1
cd "$DIR" || { osascript -e 'display alert "Isma3 folder not found" message "Expected it at '"$DIR"'."'; exit 1; }
PORT=$(.venv/bin/python -c "import json,os;c={};[c.update(json.load(open(f))) for f in ('config.example.json','config.json') if os.path.exists(f)];print(c.get('port',8765))" 2>/dev/null || echo 8765)
URL="http://localhost:$PORT"
if ! curl -s "$URL/api/health" >/dev/null; then
  curl -s http://127.0.0.1:11434/api/tags >/dev/null || { open -ga Ollama; sleep 4; }
  mkdir -p data
  nohup .venv/bin/python app.py >> data/isma3.log 2>&1 &
  for i in $(seq 1 40); do curl -s "$URL/api/health" >/dev/null && break; sleep 0.5; done
fi
if [ -d "/Applications/Google Chrome.app" ]; then
  open -na "Google Chrome" --args --app="$URL"   # its own window, no tabs
else
  open "$URL"
fi
