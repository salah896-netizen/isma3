#!/bin/bash
# Isma3 one-time installer. Double-click this file in Finder. It opens Terminal and does every setup step.
cd "$(dirname "$0")"
trap 'echo; echo "Something went wrong above. Copy the last few lines and send them to whoever set Isma3 up for you."; read -p "Press Return to close."' ERR
set -eE

if [ "$(uname -m)" != "arm64" ]; then echo "Isma3 needs a Mac with Apple Silicon (M1 or newer)."; read -p "Press Return to close."; exit 1; fi

if [ ! -x /opt/homebrew/bin/brew ]; then
  echo "== 0/5  Installing Homebrew (the standard Mac installer for developer tools). It will ask for your Mac password =="
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  grep -q 'brew shellenv' ~/.zprofile 2>/dev/null || echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
fi
eval "$(/opt/homebrew/bin/brew shellenv)"

if [ ! -f config.json ]; then
  echo
  read -p "Your first name (Isma3 uses it to greet you; press Return to skip): " NAME
  read -p "Obsidian vault folder to save notes into (e.g. ~/Documents/MyVault; press Return to skip): " VAULT
  NAME="$NAME" VAULT="$VAULT" /usr/bin/python3 -c 'import json,os;json.dump({"name":os.environ["NAME"],"obsidian_vault":os.environ["VAULT"]},open("config.json","w"),indent=2)'
fi

echo "== 1/5  Installing ffmpeg (audio converter) and Python 3.12 =="
brew install ffmpeg python@3.12

echo "== 2/5  Installing BlackHole (lets Isma3 hear call audio). It may ask for your Mac password =="
brew install --cask blackhole-2ch || echo "BlackHole skipped. Mic recording still works, so you can add it later."

if [ ! -d /Applications/Ollama.app ] && ! command -v ollama >/dev/null; then
  echo "== Installing Ollama (runs the AI models on your Mac) =="
  brew install --cask ollama-app 2>/dev/null || brew install --cask ollama 2>/dev/null || { echo "Please install Ollama from https://ollama.com/download, open it once, then double-click install.command again."; read -p "Press Return to close."; exit 1; }
fi

echo "== 3/5  Creating Isma3's private Python environment and installing its parts =="
[ -d .venv ] || /opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt

echo "== 4/5  Downloading the transcription and speaker models (about 1.7 GB) =="
./download_models.sh

echo "== 5/5  Downloading the summary and search models with Ollama (about 9 GB) =="
OLLAMA=$(command -v ollama || echo /Applications/Ollama.app/Contents/Resources/ollama)
curl -s http://127.0.0.1:11434/api/tags >/dev/null || { open -a Ollama; sleep 8; }
"$OLLAMA" pull qwen3:14b
"$OLLAMA" pull nomic-embed-text

echo "== Adding Isma3 to your Applications folder =="
./make_app.sh

echo
echo "All done. Open Isma3 from Spotlight (⌘ Space, type Isma3) or ~/Applications, and drag it to your Dock."
read -p "Press Return to close."
