#!/usr/bin/env bash
# Lance TokenbarV2.app via LaunchServices (nécessaire sur macOS 26+)

APP="/Users/julesyzerd/Applications/TokenbarV2.app"

if pgrep -f "tokenbar_v2.py" > /dev/null 2>&1; then
    echo "tokenbar-v2 tourne déjà"
    exit 0
fi

open "$APP"
echo "tokenbar-v2 démarré"
