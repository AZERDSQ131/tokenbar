#!/usr/bin/env bash
# Lance Tokenbar.app via LaunchServices (nécessaire sur macOS 26+)

APP="/Users/julesyzerd/Applications/Tokenbar.app"

if pgrep -f "tokenbar.py" > /dev/null 2>&1; then
    echo "tokenbar tourne déjà"
    exit 0
fi

open "$APP"
echo "tokenbar démarré"
