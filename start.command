#!/bin/bash
# Double-click (or run ./start.command) to start Isma3. Close this window to stop it.
cd "$(dirname "$0")"
PORT=$(.venv/bin/python -c "import json,os;c={};[c.update(json.load(open(f))) for f in ('config.example.json','config.json') if os.path.exists(f)];print(c.get('port',8765))")
if ! curl -s http://127.0.0.1:11434/api/tags >/dev/null; then
  echo "Starting Ollama..."; open -a Ollama 2>/dev/null; sleep 4
fi
export HF_HUB_OFFLINE=1   # never reach out to the internet for models once they're downloaded
(sleep 2; open "http://localhost:$PORT") &
echo "Isma3 is running at http://localhost:$PORT  (close this window to stop it)"
exec .venv/bin/python app.py
