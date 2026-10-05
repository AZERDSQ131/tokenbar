#!/usr/bin/env python3
"""Export incremental des tokens Jetson vers v2/jetson_data/.

Source : ~/.opencodex/usage.jsonl (une ligne JSON par requete proxiee).
Sortie : jetson_usage_YYYY-MM.jsonl append-only (un fichier par mois calendaire
local, bucketise sur le timestamp du record), transporte par hub-sync vers le
Mac ou TokenBar les fusionne de facon strictement additive.

Curseur : ~/.local/share/tokenbar-jetson/cursor.json
  {offset, size, ino, mtime, seen:[requestIds recents]}.
  - Reprise a l'offset (pas de relecture du fichier entier).
  - Rotation/troncature detectee (inode change ou taille < offset) -> repart a 0,
    les requestIds deja vus (seen) evite tout doublon.
  - Crash entre append et save curseur : au rerun, les ids deja exportes sont
    dans seen -> ignores (idempotent).

Robustesse : lignes malformees ou sans requestId ignorees (comptees sur stderr),
jamais de crash. Stdlib uniquement.

Usage :
  python3 v2/jetson_export.py                       # run incremental
  python3 v2/jetson_export.py --reset               # purge curseur+exports puis re-export complet
  python3 v2/jetson_export.py --src F --out D --cursor C   # overrides (tests)
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SRC_DEFAULT = Path.home() / ".opencodex/usage.jsonl"
CURSOR_DEFAULT = Path.home() / ".local/share/tokenbar-jetson/cursor.json"
OUT_DEFAULT = Path(__file__).resolve().parent / "jetson_data"

# Champs conserves dans l'export (le reste : attempts, routeDecision,
# accountLogLabel, conversationId... est jete pour rester leger).
KEEP_FIELDS = ("requestId", "timestamp", "model", "surface", "usage", "totalTokens")

SEEN_CAP = 20000


def load_cursor(path):
    try:
        d = json.loads(Path(path).read_text())
        return {"offset": int(d.get("offset", 0)),
                "size": int(d.get("size", 0)),
                "ino": d.get("ino"),
                "mtime": float(d.get("mtime", 0.0)),
                "seen": list(d.get("seen", []))}
    except Exception:
        return {"offset": 0, "size": 0, "ino": None, "mtime": 0.0, "seen": []}


def save_cursor(path, cur):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur))
    os.replace(tmp, p)


def _month_of(ts):
    """Mois calendaire local (YYYY-MM) d'un timestamp epoch ms."""
    try:
        return datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m")
    except Exception:
        return datetime.now().strftime("%Y-%m")


def export(src, out_dir, cursor_path):
    """Lit les octets nouveaux de src, append les records inconnus. Retourne stats."""
    src, out_dir = Path(src), Path(out_dir)
    if not src.exists():
        raise FileNotFoundError(f"source introuvable: {src}")
    out_dir.mkdir(parents=True, exist_ok=True)
    cur = load_cursor(cursor_path)
    seen = set(cur.get("seen", []))

    st = src.stat()
    offset = cur.get("offset", 0)
    # Rotation / remplacement / troncature : on repart a 0, seen dedupera.
    if cur.get("ino") is not None and (st.st_ino != cur["ino"] or st.st_size < offset):
        offset = 0

    exported, dupes, malformed = 0, 0, 0
    handles = {}

    def _handle(month):
        h = handles.get(month)
        if h is None:
            h = open(out_dir / f"jetson_usage_{month}.jsonl", "a", encoding="utf-8")
            handles[month] = h
        return h

    try:
        with open(src, encoding="utf-8", errors="ignore") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    malformed += 1
                    continue
                rid = e.get("requestId")
                if not rid:
                    malformed += 1
                    continue
                if rid in seen:
                    dupes += 1
                    continue
                seen.add(rid)
                rec = {k: e.get(k) for k in KEEP_FIELDS}
                _handle(_month_of(e.get("timestamp"))).write(json.dumps(rec) + "\n")
                exported += 1
            new_offset = f.tell()
    finally:
        for h in handles.values():
            h.close()

    # Si le fichier n'a pas bougé (ou a ete lu en entier), l'offset = taille.
    # new_offset vaut la position apres lecture = st.st_size sauf ecriture concurrente.
    cur = {"offset": new_offset if 'new_offset' in locals() else st.st_size,
           "size": st.st_size,
           "ino": st.st_ino,
           "mtime": st.st_mtime,
           "seen": sorted(seen)[-SEEN_CAP:]}
    save_cursor(cursor_path, cur)
    return {"exported": exported, "dupes": dupes, "malformed": malformed,
            "offset": cur["offset"], "size": st.st_size}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export incremental usage Jetson")
    ap.add_argument("--src", default=str(SRC_DEFAULT))
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--cursor", default=str(CURSOR_DEFAULT))
    ap.add_argument("--reset", action="store_true",
                    help="purge curseur + exports puis re-export complet")
    a = ap.parse_args(argv)
    if a.reset:
        try:
            Path(a.cursor).unlink()
        except FileNotFoundError:
            pass
        for f in Path(a.out).glob("jetson_usage_*.jsonl") if Path(a.out).exists() else []:
            f.unlink()
        print("[jetson-export] reset: curseur + exports purges", flush=True)
    try:
        s = export(a.src, a.out, a.cursor)
    except FileNotFoundError as e:
        print(f"[jetson-export] {e}", file=sys.stderr, flush=True)
        return 1
    print(f"[jetson-export] +{s['exported']} dupes={s['dupes']} "
          f"malformed={s['malformed']} offset={s['offset']}/{s['size']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
