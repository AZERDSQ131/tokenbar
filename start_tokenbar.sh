#!/usr/bin/env bash
# Lance tokenbar.py en arrière-plan et loggue dans /tmp/tokenbar.log

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="/tmp/tokenbar.pid"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "tokenbar tourne déjà (PID $(cat "$PID_FILE"))"
    exit 0
fi

nohup /opt/homebrew/bin/python3.12 "$SCRIPT_DIR/tokenbar.py" > /tmp/tokenbar.log 2>&1 &
echo $! > "$PID_FILE"
echo "tokenbar démarré (PID $!)"
