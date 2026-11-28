#!/bin/bash
# Installation sur le serveur (Ubuntu 24.04, ARM ou x86): bash setup.sh
# Crée .venv et installe ce que font record.yml et summary.yml.
set -euo pipefail
cd "$(dirname "$0")"
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg python3-venv python3-dev build-essential cmake git
python3 -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
# record.yml
pip install -q requests
# summary.yml
pip install -q faster-whisper anthropic requests huggingface_hub imageio-ffmpeg mutagen pillow
if grep -Eq '^summary_engine *= *(local|both)' settings.txt; then
  pip install llama-cpp-python   # compilé sur place (plusieurs minutes sur ARM)
fi
[ -f .env ] || cp .env.example .env
chmod 600 .env
echo "Installé. Remplis .env, puis teste avec: ./run.sh 5"
