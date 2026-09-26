# Tokenbar v2 — 100 % Pi (ton harness)

Menu bar macOS qui affiche ta consommation pi en direct depuis les sessions
locales (`~/.pi/agent/sessions/*/*.jsonl`). Aucune estimation : pi calcule
lui-même le coût exact de chaque message.

![macOS only](https://img.shields.io/badge/macOS-only-black?logo=apple)
![Python 3](https://img.shields.io/badge/Python-3-blue)

## Interface (reconstruite de zéro)

- **Header** `π Pi` + nombre de sessions (total · aujourd'hui), heure de MAJ
- **Stats** : Today (avec temps écoulé) · 7 days · All time · Cost today (exact)
- **Graphiques** : tokens 30 j + coûts, périodes 1d/7d/1m/All, styles
  bars/line/area, tooltips
- **Top models** : top 8 inline avec barres + fenêtre « All models »
  (1d/7d/1m/all, recherche, coûts exacts)
- **Quota mensuel** ($, barre de progression), **Flex** (tweet pré-rempli),
  alertes seuils, notification du soir, lancement au login

Séries calendaires continues : chaque jour sans activité vaut 0 explicite
(jamais de vieux jour présenté comme aujourd'hui).

## Données

`_pi_all_messages()` : granularité message (modèle + jour + provider exacts),
cache incrémental par fichier (mtime+taille, ~167 fichiers). Froid ~0,5 s,
tiède ~0 s. Fetch en tâche de fond toutes les 15 s, jamais sur le thread UI.

## Lancement

```bash
./start_tokenbar_v2.sh        # direct, menu bar π (pas via open : le bundle .app n'affiche pas l'icône)
/opt/homebrew/bin/python3.12 v2/tokenbar_v2.py   # premier plan (debug)
```

Prérequis : `pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit`.

Réglages : `~/.tokenbar_v2_settings.json` (active le lancement au login pour la persistance). Logs : `/tmp/tokenbar_v2.log`.
Historique multi-source (proxy/CLI) : voir git (`/tmp/tokenbar_v2_multi_backup.py`
pour la version précédente).
