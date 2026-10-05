"""Source Jetson pour TokenBar v2 (cote Mac).

Lit les exports versionnes v2/jetson_data/jetson_usage_YYYY-MM.jsonl (produits
par v2/jetson_export.py sur le Jetson, transportes par hub-sync) et les
convertit en rows provider "jetson" : (day, model, provider, i, o, r, cr, cw,
cost) — meme shape que _pi/_codex/_claude_all_messages().

Regles :
- cout via claude_cost() importe de tokenbar_v2 (jamais duplique) ;
- dedupe par requestId (globale, pas par fichier) : sur re-export/overlap,
  chaque requete ne compte qu'une fois ;
- jour en fuseau local (timestamp epoch ms) ;
- lignes malformees ignorees silencieusement.

Importe sans PyObjC : claude_cost est importe paresseusement dans
jetson_all_messages() (avec fallback sys.path sur le dossier parent,
car selon le CWD v2/ n'est pas forcement dans sys.path) pour que
v2/test_jetson.py tourne sur Linux.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

JETSON_DIR = Path(__file__).resolve().parent / "jetson_data"
PROVIDER = "jetson"

# files: path -> {"sig": (mtime, size), "rows": [...], "ids": [...]}
_jetson_cache = {"files": {}, "rows": []}


def _day(ts_ms):
    """Jour local (YYYY-MM-DD) d'un timestamp epoch ms, None si invalide."""
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return None


def _parse_record(e, seen, cost_fn):
    """Convertit un record export en row, ou None si a ignorer. Idempotent.

    Ignore : non-dict, sans requestId, deja vu, sans usage (ex: requetes
    en erreur HTTP 400 avec usage=null dans le vrai usage.jsonl),
    compteurs non-entiers, jour ou total de tokens invalide.
    """
    if not isinstance(e, dict):
        return None
    rid = e.get("requestId")
    if not rid or rid in seen:
        return None
    u = e.get("usage")
    if not isinstance(u, dict):
        return None
    try:
        i = int(u.get("inputTokens", 0))
        o = int(u.get("outputTokens", 0))
        cr = int(u.get("cachedInputTokens", 0))
        r = int(u.get("reasoningOutputTokens", 0))
    except Exception:
        return None
    # inputTokens inclut deja le cache (verifie sur le vrai usage.jsonl :
    # totalTokens == inputTokens + outputTokens) -> input net pour
    # tok = i_net+o+cr+cw = total reel.
    i_net = max(0, i - cr)
    if not (i_net or o or cr):
        return None
    day = _day(e.get("timestamp"))
    if day is None:
        return None
    seen.add(rid)
    model = e.get("model") or "jetson"
    cst = cost_fn(model, i_net, o, 0, cr)
    return (day, model, PROVIDER, i_net, o, r, cr, 0, cst)


def jetson_all_messages(data_dir=None, cost_fn=None):
    """Liste (day, model, provider, i, o, r, cr, cw, cost), cache incremental."""
    if cost_fn is None:
        if str(Path(__file__).resolve().parent) not in sys.path:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
        from tokenbar_v2 import claude_cost as cost_fn
    base = Path(data_dir) if data_dir else JETSON_DIR
    if not base.exists():
        return list(_jetson_cache["rows"])
    try:
        files = sorted(base.glob("jetson_usage_*.jsonl"))
    except Exception as e:
        print(f"[tokenbar-v2] jetson scan: {e}", flush=True)
        return list(_jetson_cache["rows"])
    files_state = _jetson_cache["files"]
    live = set()
    changed = False
    for jf in files:
        p = str(jf)
        live.add(p)
        try:
            st = jf.stat()
            sig = (st.st_mtime, st.st_size)
        except Exception:
            continue
        entry = files_state.get(p)
        if isinstance(entry, dict) and entry.get("sig") == sig:
            continue
        changed = True
        rows, seen, ids = [], set(), []
        try:
            with open(jf, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    row = _parse_record(e, seen, cost_fn)
                    if row:
                        rows.append(row)
                        ids.append(e.get("requestId"))
        except Exception:
            if p in files_state:
                del files_state[p]
            continue
        files_state[p] = {"sig": sig, "rows": rows, "ids": ids}
    for p in [k for k in files_state if k not in live]:
        del files_state[p]
        changed = True
    if changed:
        # Dedupe globale par requestId (ordre de fichier : le 1er qui declare
        # un rid gagne). Les rows sont zippees avec leurs ids par index.
        seen_all, out = set(), []
        for p in sorted(files_state):
            rows = files_state[p]["rows"]
            ids = files_state[p]["ids"]
            for idx, row in enumerate(rows):
                rid = ids[idx] if idx < len(ids) else None
                if rid is None or rid in seen_all:
                    continue
                seen_all.add(rid)
                out.append(row)
        _jetson_cache["rows"] = out
    return list(_jetson_cache["rows"])


def reset_cache():
    """Vide le cache (tests)."""
    _jetson_cache["files"] = {}
    _jetson_cache["rows"] = []
