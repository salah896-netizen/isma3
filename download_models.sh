#!/bin/bash
# Downloads the offline models Isma3 needs. Run once (needs internet), then everything works offline.
set -e
cd "$(dirname "$0")"
mkdir -p models
BASE=https://github.com/k2-fsa/sherpa-onnx/releases/download

if [ ! -f models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx ]; then
  echo "1/3  Speaker-change detector (7 MB)..."
  curl -L --fail -o models/seg.tar.bz2 "$BASE/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
  tar xjf models/seg.tar.bz2 -C models && rm models/seg.tar.bz2
fi

if [ ! -f models/nemo_en_titanet_small.onnx ]; then
  echo "2/3  Voice fingerprint model (40 MB)..."
  curl -L --fail -o models/nemo_en_titanet_small.onnx "$BASE/speaker-recongition-models/nemo_en_titanet_small.onnx"
fi

echo "3/3  Whisper transcription model (about 1.6 GB, the big one)..."
.venv/bin/python - <<'PY'
import json
from huggingface_hub import snapshot_download
import os
cfg = {}
for f in ("config.example.json", "config.json"):
    if os.path.exists(f):
        cfg.update(json.load(open(f)))
repo = cfg.get("whisper_model", "mlx-community/whisper-large-v3-turbo")
snapshot_download(repo)
print("Whisper model ready:", repo)
PY
echo "Done. Speaker and transcription models are saved on this Mac."
