#!/usr/bin/env bash
# ClipWatch: starts the dashboard on http://127.0.0.1:8792 (Ctrl+C stops it)
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then echo "First run ./setup.sh"; exit 1; fi
PORT=$(.venv/bin/python -c "import json;print(json.load(open('config.json')).get('port',8792))" 2>/dev/null || echo 8792)
( sleep 2; URL="http://127.0.0.1:$PORT"; command -v open >/dev/null && open "$URL" || xdg-open "$URL" ) >/dev/null 2>&1 &
exec .venv/bin/python server.py "$@"
