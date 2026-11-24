"""SQLite store for moments and the persisted watchlist."""
import json
import sqlite3
import threading
import time

from .config import DATA

_db = sqlite3.connect(DATA / "clipwatch.db", check_same_thread=False)
_db.row_factory = sqlite3.Row
_lock = threading.Lock()

with _lock:
    _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS moments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          stream_id TEXT, platform TEXT, channel TEXT, title TEXT, url TEXT,
          at REAL, score REAL, tag TEXT, reasons TEXT, chat TEXT,
          status TEXT DEFAULT 'detected',
          clip TEXT, thumb TEXT, vertical TEXT, error TEXT,
          created REAL
        );
        CREATE TABLE IF NOT EXISTS watchlist (
          id TEXT PRIMARY KEY, url TEXT, platform TEXT, channel TEXT, title TEXT, added REAL
        );
        CREATE TABLE IF NOT EXISTS rules (
          key TEXT PRIMARY KEY, name TEXT, platform TEXT, url TEXT, opts TEXT, updated REAL
        );
        CREATE TABLE IF NOT EXISTS facecams (
          key TEXT PRIMARY KEY, platform TEXT, channel TEXT, mode TEXT,
          x REAL, y REAL, w REAL, h REAL, updated REAL
        );
        """
    )
    have = {r[1] for r in _db.execute("PRAGMA table_info(moments)").fetchall()}
    for col, typ in (("hook", "TEXT"), ("style", "TEXT"), ("edit_state", "TEXT"), ("edit_info", "TEXT"),
                     ("kind", "TEXT"), ("posted_at", "REAL"), ("cleaned", "REAL"),
                     ("opened_at", "REAL"), ("asked_at", "REAL"),
                     ("plan", "TEXT"), ("ai_ideas", "TEXT"), ("ai_state", "TEXT"), ("ai_request", "TEXT")):
        if col not in have:
            _db.execute(f"ALTER TABLE moments ADD COLUMN {col} {typ}")
    # jobs live in memory: anything cut or edited when the server stopped won't finish by itself
    _db.execute("UPDATE moments SET status='failed', error='interrupted by a restart' WHERE status='clipping'")
    _db.execute("UPDATE moments SET edit_state='skipped' WHERE edit_state IN ('queued', 'editing')")
    _db.execute("UPDATE moments SET ai_state=NULL WHERE ai_state='thinking'")
    if "auto" not in {r[1] for r in _db.execute("PRAGMA table_info(watchlist)").fetchall()}:
        _db.execute("ALTER TABLE watchlist ADD COLUMN auto INTEGER DEFAULT 0")
    _db.commit()


def _row(r):
    d = dict(r)
    for k in ("reasons", "chat"):
        try:
            d[k] = json.loads(d[k]) if d[k] else []
        except ValueError:
            d[k] = []
    for k, empty in (("edit_info", {}), ("plan", {}), ("ai_ideas", [])):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else empty
        except ValueError:
            d[k] = empty
    return d


def add_moment(**m):
    with _lock:
        cur = _db.execute(
            "INSERT INTO moments (stream_id, platform, channel, title, url, at, score, tag, reasons, chat, status, created, kind)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                m["stream_id"], m["platform"], m["channel"], m.get("title", ""), m.get("url", ""),
                m["at"], m["score"], m["tag"], json.dumps(m.get("reasons", [])),
                json.dumps(m.get("chat", []), ensure_ascii=False), m.get("status", "detected"), time.time(),
                m.get("kind", "short"),
            ),
        )
        _db.commit()
        return cur.lastrowid


def update_moment(mid, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _db.execute(f"UPDATE moments SET {cols} WHERE id=?", (*fields.values(), mid))
        _db.commit()


def get_moment(mid):
    with _lock:
        r = _db.execute("SELECT * FROM moments WHERE id=?", (mid,)).fetchone()
    return _row(r) if r else None


def list_moments(limit=200):
    with _lock:
        rows = _db.execute("SELECT * FROM moments ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def watch_add(s):
    with _lock:
        _db.execute(
            "INSERT OR REPLACE INTO watchlist (id, url, platform, channel, title, added, auto) VALUES (?,?,?,?,?,?,?)",
            (s["id"], s["url"], s["platform"], s["channel"], s.get("title", ""), time.time(), int(bool(s.get("auto")))),
        )
        _db.commit()


def watch_remove(sid):
    with _lock:
        _db.execute("DELETE FROM watchlist WHERE id=?", (sid,))
        _db.commit()


def watch_list():
    with _lock:
        rows = [dict(r) for r in _db.execute("SELECT * FROM watchlist ORDER BY added").fetchall()]
    for r in rows:
        r["auto"] = bool(r.get("auto"))
    return rows


def channel_stats():
    """Per channel: how many moments, how many you kept / rejected (feeds the scout ranking)."""
    with _lock:
        rows = _db.execute(
            "SELECT CASE WHEN platform='youtube' THEN 'youtube:@' || lower(channel) ELSE stream_id END k,"
            " COUNT(*) n, SUM(status='kept') kept, SUM(status='rejected') rejected FROM moments GROUP BY k"
        ).fetchall()
    return {r["k"]: {"n": r["n"], "kept": r["kept"] or 0, "rejected": r["rejected"] or 0} for r in rows}


def history_key(platform, stream_id, channel):
    """YouTube gets a new id every stream, so its history is kept per channel name."""
    return f"youtube:@{(channel or '').lower()}" if platform == "youtube" else stream_id


# ---- facecam boxes you draw once per streamer (mode "box"), or "none" for streamers without a webcam
def facecam_key(platform, stream_id, channel):
    """One facecam per streamer. YouTube ids change every stream, so YouTube goes by channel name."""
    if platform == "youtube":
        c = (channel or "").strip().lstrip("@").lower()
        if c:
            return f"youtube:@{c}"
    return stream_id


def facecam_get(key):
    with _lock:
        r = _db.execute("SELECT * FROM facecams WHERE key=?", (key,)).fetchone()
    return dict(r) if r else None


def facecam_set(key, platform, channel, mode, box=None):
    x, y, w, h = box if box else (None, None, None, None)
    with _lock:
        _db.execute(
            "INSERT OR REPLACE INTO facecams (key, platform, channel, mode, x, y, w, h, updated) VALUES (?,?,?,?,?,?,?,?,?)",
            (key, platform, channel, mode, x, y, w, h, time.time()),
        )
        _db.commit()


def facecam_clear(key):
    with _lock:
        _db.execute("DELETE FROM facecams WHERE key=?", (key,))
        _db.commit()


def facecam_all():
    with _lock:
        rows = _db.execute("SELECT * FROM facecams").fetchall()
    return {r["key"]: dict(r) for r in rows}


# ---- clip rules per streamer (the Rules board). Missing values fall back to the defaults in config.json.
RULE_FIELDS = {"record": bool, "auto_clip": bool, "threshold": float, "length": str, "style": str,
               "auto_edit": bool, "max_per_hour": int}
LENGTHS = ("short", "long", "best", "auto")


def rules_all():
    with _lock:
        rows = _db.execute("SELECT * FROM rules").fetchall()
    out = {}
    for r in rows:
        try:
            opts = json.loads(r["opts"] or "{}")
        except ValueError:
            opts = {}
        out[r["key"]] = {"key": r["key"], "name": r["name"], "platform": r["platform"], "url": r["url"], "opts": opts}
    return out


def rule_set(key, patch, name=None, platform=None, url=None):
    """Merge changes into a streamer's rule; a value of None goes back to the default."""
    cur = rules_all().get(key) or {"opts": {}}
    opts = dict(cur["opts"])
    for k, v in patch.items():
        if k not in RULE_FIELDS:
            continue
        if v is None or v == "":
            opts.pop(k, None)
            continue
        typ = RULE_FIELDS[k]
        try:
            v = typ(v) if typ is not bool else bool(v)
        except (TypeError, ValueError):
            continue
        if k == "length" and v not in LENGTHS:
            continue
        if k == "threshold":
            v = min(max(v, 0.5), 20.0)
        if k == "max_per_hour":
            v = min(max(v, 0), 60)
        opts[k] = v
    with _lock:
        _db.execute("INSERT OR REPLACE INTO rules (key, name, platform, url, opts, updated) VALUES (?,?,?,?,?,?)",
                    (key, name or cur.get("name"), platform or cur.get("platform"), url or cur.get("url"),
                     json.dumps(opts), time.time()))
        _db.commit()
    return opts


def effective_rule(cfg, key, rules=None):
    """A streamer's rule with the defaults filled in."""
    o = ((rules if rules is not None else rules_all()).get(key) or {}).get("opts", {})
    return {
        "record": o.get("record", True),
        "auto_clip": o.get("auto_clip", cfg.get("auto_clip", True)),
        "threshold": float(o.get("threshold", cfg["threshold"])),
        "length": o.get("length", cfg.get("highlight_length", "best")),
        "style": o.get("style", cfg.get("edit_style", "streamer")),
        "auto_edit": o.get("auto_edit", cfg.get("auto_edit", True)),
        "max_per_hour": int(o.get("max_per_hour", cfg.get("max_per_hour", 0)) or 0),
    }
