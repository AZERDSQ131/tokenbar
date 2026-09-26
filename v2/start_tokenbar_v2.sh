#!/usr/bin/env bash
# Lance tokenbar-v2 en direct (le bundle .app via `open` n'affiche pas l'icône).
LOG="/tmp/tokenbar_v2.log"

if pgrep -f "tokenbar_v2.py" > /dev/null 2>&1; then
    echo "tokenbar-v2 tourne déjà"
    exit 0
fi

cd /Users/julesyzerd/projects/tokenbar
nohup /opt/homebrew/bin/python3.12 v2/tokenbar_v2.py >>"$LOG" 2>&1 &
echo "tokenbar-v2 démarré"
