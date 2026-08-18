#!/bin/bash
# Bouge légèrement la souris toutes les N secondes pour empêcher la mise en veille

INTERVAL="${1:-60}"

echo "Démarré — mouvement toutes les ${INTERVAL}s. Ctrl+C pour arrêter."

while true; do
    OUT=$(python3 - <<'PYEOF'
from AppKit import NSEvent, NSScreen
from Quartz import CGWarpMouseCursorPosition

loc = NSEvent.mouseLocation()
screen = NSScreen.screens()[0].frame()
x, y = loc.x, screen.size.height - loc.y

CGWarpMouseCursorPosition((x + 1, y))
CGWarpMouseCursorPosition((x, y))

print(f"{x:.0f} {y:.0f}")
PYEOF
    )
    read -r X Y <<< "$OUT"

    echo "$(date '+%H:%M:%S') — souris bougée en (${X}, ${Y})"
    sleep "$INTERVAL"
done
