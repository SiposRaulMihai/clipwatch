#!/usr/bin/env bash
# ClipWatch setup for macOS and Linux. Safe to run again.
set -e
cd "$(dirname "$0")"
say() { printf "\n\033[1m%s\033[0m\n" "$1"; }

say "1/4 ffmpeg"
if ! command -v ffmpeg >/dev/null; then
  if command -v brew >/dev/null; then brew install ffmpeg
  elif command -v apt-get >/dev/null; then sudo apt-get update && sudo apt-get install -y ffmpeg fonts-dejavu-core fonts-noto-color-emoji
  elif command -v dnf >/dev/null; then sudo dnf install -y ffmpeg dejavu-sans-fonts google-noto-emoji-color-fonts
  else echo "Install ffmpeg (https://ffmpeg.org/download.html) and run this again."; exit 1; fi
fi
ffmpeg -hide_banner -filters 2>/dev/null | grep -q xfade || echo "Note: your ffmpeg is old (no xfade); compilations need ffmpeg 4.3+."

say "2/4 Python 3.10+"
PY=""
for p in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v $p >/dev/null && $p -c 'import sys; sys.exit(sys.version_info < (3,10))'; then PY=$p; break; fi
done
if [ -z "$PY" ]; then echo "Python 3.10 or newer is needed (macOS: brew install python@3.12)."; exit 1; fi
echo "using $($PY --version)"

say "3/4 Python packages (whisper, streamlink, yt-dlp…)"
[ -d .venv ] || $PY -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt

say "4/4 Settings"
[ -f .env ] || cp .env.example .env
.venv/bin/python -c "import cw.config" && echo "config.json ready (your settings; edit in the dashboard)"
if [ "$(uname)" = "Darwin" ] && ! command -v swiftc >/dev/null; then
  echo "Tip: run 'xcode-select --install' so ClipWatch can find streamers' webcams automatically."
fi

say "Done. Start it with:  ./run.sh"
