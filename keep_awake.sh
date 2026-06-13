#!/bin/bash
# Bouge légèrement la souris toutes les 60s pour empêcher la mise en veille

INTERVAL=60

echo "Démarré — mouvement toutes les ${INTERVAL}s. Ctrl+C pour arrêter."

while true; do
    # Récupère la position actuelle
    POS=$(osascript -e 'tell application "System Events" to return (position of (the mouse))')
    X=$(echo "$POS" | awk -F',' '{print $1}' | tr -d ' ')
    Y=$(echo "$POS" | awk -F',' '{print $2}' | tr -d ' ')

    # Bouge d'un pixel à droite puis revient
    osascript -e "tell application \"System Events\" to set the mouse to {$((X+1)), ${Y}}"
    sleep 0.1
    osascript -e "tell application \"System Events\" to set the mouse to {${X}, ${Y}}"

    echo "$(date '+%H:%M:%S') — souris bougée en (${X}, ${Y})"
    sleep "$INTERVAL"
done
