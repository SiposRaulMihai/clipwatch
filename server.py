"""ClipWatch local server — http://127.0.0.1:8792 (localhost only)."""
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cw import ai, clipper, config, editor, store
from cw.engine import Engine

UI = config.ROOT / "ui"

# tools installed into the virtualenv (yt-dlp, streamlink) are found even when it isn't activated
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
for tool in ("ffmpeg", "ffprobe", "yt-dlp", "streamlink"):
    if not shutil.which(tool):
        sys.exit(f"{tool} is missing. Run ./setup.sh (or see README: Install).")

try:
    _moved = clipper.migrate_layout()  # one folder per streamer (older clips were per date)
    if _moved:
        print(f"Moved {_moved} older files into streamer folders")
except OSError as e:
    print("Couldn't reorganise older clips:", e)
engine = Engine()
STYLES = [{"key": k, "label": v["label"], "about": v["about"], "layout": v["layout"], "face_h": v.get("face_h")}
          for k, v in editor.STYLES.items()]
FRAMES = config.DATA / "frames"


def _grab(src, t, out):
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=1280:-2", "-q:v", "3", str(out)], capture_output=True, timeout=30)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 0


def frame_for(q):
    """A still to draw the facecam box on: from a clip (at t seconds) or the newest second of a live stream."""
    if q.get("mid"):
        m = store.get_moment(int(q["mid"]))
        if not m or not m.get("clip") or not Path(m["clip"]).exists():
            raise ValueError("clip not ready")
        dur = editor.probe(m["clip"])["dur"]
        t = float(q["t"]) if q.get("t") else (m.get("edit_info") or {}).get("peak") or dur * 0.6
        t = t % max(1.0, dur - 0.6)
        out = FRAMES / f"m{m['id']}.jpg"
        if _grab(m["clip"], t, out):
            return out, round(t, 2)
        raise ValueError("couldn't read a frame from that clip")
    st = engine.streams.get(q.get("sid") or "")
    if not st:
        raise ValueError("that stream isn't being watched")
    segs = st["rec"].segments()
    for seg in (segs[-2:-1] + segs[-1:] if len(segs) > 1 else segs):
        out = FRAMES / ("live_" + re.sub(r"[^\w-]", "_", st["meta"]["id"]) + ".jpg")
        if _grab(seg, 1.0, out):
            return out, None
    raise ValueError("no video buffered yet — try again in a few seconds")


def cleanup(b):
    """Delete the video files of posted (and, if ticked, rejected) clips. dry=True only measures."""
    which = {w for w in b.get("which", ["posted"]) if w in ("posted", "rejected")}
    sizes = {"posted": {"clips": 0, "bytes": 0}, "rejected": {"clips": 0, "bytes": 0}}
    plan = []
    for m in store.list_moments(100000):
        if m["status"] not in sizes or m.get("cleaned"):
            continue
        files = clipper.clip_files(m)
        size = sum(f.stat().st_size for f in files)
        if not files:
            continue
        sizes[m["status"]]["clips"] += 1
        sizes[m["status"]]["bytes"] += size
        if m["status"] in which:
            plan.append((m["id"], files))
    if b.get("dry", True):
        return {"dry": True, **sizes}
    freed = deleted = 0
    for mid, _ in plan:
        n, nbytes = clipper.delete_clip_files(mid)
        deleted += n
        freed += nbytes
    engine._disk_at = 0  # re-measure free space now
    return {"dry": False, "clips": len(plan), "files": deleted, "bytes": freed}


def board():
    """Rows for the Rules board: everyone you watch, have a rule for, marked as favorite, or the scout sees live."""
    cfg = config.load()
    rules = store.rules_all()
    rows = {}

    def add(key, name, platform, url=None, **extra):
        r = rows.setdefault(key, {"key": key, "name": name, "platform": platform, "url": url, "watching": False,
                                  "live": False, "status": "", "score": None, "clips_hour": 0, "fav": False})
        r["url"] = r["url"] or url
        r.update({k: v for k, v in extra.items() if v is not None})

    for f in cfg.get("favorites", []):
        sid = f"{f['platform']}:{f['channel'].lower()}" if f["platform"] != "youtube" else ""
        add(store.facecam_key(f["platform"], sid, f.get("name") or f["channel"]), f.get("name") or f["channel"],
            f["platform"], fav=True)
    for k, r in rules.items():
        add(k, r.get("name") or k, r.get("platform") or k.split(":")[0], r.get("url"))
    for r in engine.scout.ranked:
        add(r["key"], r.get("display") or r["channel"], r["platform"], r.get("url"), live=True, viewers=r.get("viewers"))
    for s in engine.snapshot():
        key = store.facecam_key(s["platform"], s["id"], s["channel"])
        add(key, s["channel"], s["platform"], s["url"], watching=True, live=s["status"] == "live", status=s["status"],
            sid=s["id"], score=(s.get("last") or {}).get("score"), clips_hour=s.get("clips_hour", 0))
    for w in store.watch_list():
        key = store.facecam_key(w["platform"], w["id"], w["channel"])
        if not rows.get(key, {}).get("watching"):
            add(key, w["channel"], w["platform"], w["url"], listed=True)  # on the watch list (ClipWatch off or waiting)
    out = []
    for r in rows.values():
        r["opts"] = (rules.get(r["key"]) or {}).get("opts", {})
        r["rule"] = store.effective_rule(cfg, r["key"], rules)
        out.append(r)
    out.sort(key=lambda r: (not r["watching"], not r["live"], not r["fav"], (r["name"] or "").lower()))
    return out


def set_rule(b):
    key = b["key"]
    patch = {k: b[k] for k in store.RULE_FIELDS if k in b}
    opts = store.rule_set(key, patch, name=b.get("name"), platform=b.get("platform"), url=b.get("url"))
    if "record" in patch:
        sids = [sid for sid, st in engine.streams.items() if engine.key(st) == key]
        if patch["record"] is False:
            for sid in sids:
                engine.unwatch(sid)  # stop now; autopilot won't pick this streamer again
        elif patch["record"] and not sids and b.get("url") and b.get("live"):
            engine.watch(b["url"], "", b.get("name") or "")  # turn on = record now, if they're live
    return {"ok": True, "opts": opts}


def set_facecam(b):
    mid, sid = b.get("mid"), b.get("sid")
    if mid:
        m = store.get_moment(int(mid))
        if not m:
            raise ValueError("no such clip")
        platform, stream_id, channel = m["platform"], m["stream_id"], m["channel"]
    elif sid and sid in engine.streams:
        meta = engine.streams[sid]["meta"]
        platform, stream_id, channel = meta["platform"], meta["id"], meta["channel"]
    else:
        raise ValueError("pick a clip or a stream")
    key = store.facecam_key(platform, stream_id, channel)
    mode = b.get("mode")
    if mode == "box":
        x, y, w, h = (float(v) for v in b["box"])
        if not (0 <= x < 1 and 0 <= y < 1 and 0.03 <= w <= 1 and 0.03 <= h <= 1 and x + w <= 1.002 and y + h <= 1.002):
            raise ValueError("the box has to be inside the frame")
        store.facecam_set(key, platform, channel, "box", (x, y, min(w, 1 - x), min(h, 1 - y)))
    elif mode == "none":
        store.facecam_set(key, platform, channel, "none")
    elif mode == "auto":
        store.facecam_clear(key)
    else:
        raise ValueError("unknown facecam mode")
    # re-render the clip you drew on, plus (if asked) this streamer's other clips waiting for review
    busy = ("queued", "editing")
    redo = []
    if mid:
        m = store.get_moment(int(mid))
        if m.get("clip") and m.get("edit_state") not in busy:
            redo.append(m["id"])
    if b.get("redo_all"):
        for m2 in store.list_moments(400):
            if (m2["id"] not in redo and m2.get("clip") and m2["status"] == "ready" and m2.get("edit_state") not in busy
                    and store.facecam_key(m2["platform"], m2["stream_id"], m2["channel"]) == key):
                redo.append(m2["id"])
    redo = redo[:10]  # one edit at a time on this Mac: the rest stay as they are
    for i in redo:
        clipper.queue_edit(i)
    return {"ok": True, "key": key, "redone": len(redo)}


def _media_ok(path):
    try:
        p = Path(path).resolve()
        return p.is_file() and config.output_dir().resolve() in p.parents
    except OSError:
        return False


class H(BaseHTTPRequestHandler):
    server_version = "ClipWatch/1.0"

    def log_message(self, *a):
        pass

    # ---- helpers ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _file(self, path, ctype=None):
        size = path.stat().st_size
        ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        start, end = 0, size - 1
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else end
            else:
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        for k, v in (getattr(self, "_extra", None) or {}).items():
            self.send_header(k, v)
        self._extra = None
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ---- routes ----
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._file(UI / "index.html", "text/html; charset=utf-8")
        if u.path.startswith("/ui/"):
            p = (UI / u.path[4:]).resolve()
            if UI.resolve() in p.parents and p.is_file():
                return self._file(p)
        if u.path == "/media":
            p = urllib.parse.parse_qs(u.query).get("p", [""])[0]
            if _media_ok(p):
                return self._file(Path(p))
            return self._json({"error": "not found"}, 404)
        if u.path == "/api/frame":
            try:
                out, t = frame_for({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
            except (ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
                return self._json({"error": str(e)}, 404)
            self._extra = {"X-Frame-Time": "" if t is None else str(t), "Cache-Control": "no-store"}
            return self._file(out, "image/jpeg")
        if u.path == "/api/state":
            cfg = config.load()
            streams = engine.snapshot()
            for s in streams:
                s["fc_key"] = store.facecam_key(s["platform"], s["id"], s["channel"])
            moments = store.list_moments()
            pending = engine.delete_in()
            for m in moments:
                m["fc_key"] = store.facecam_key(m["platform"], m["stream_id"], m["channel"])
                if m["id"] in pending:
                    m["delete_in"] = round(pending[m["id"]], 1)
            return self._json({
                "streams": streams,
                "moments": moments,
                "facecams": store.facecam_all(),
                "board": board(),
                "running": engine.running is not False,
                "disk": engine.disk(cfg),
                "discovered": engine.discovered,
                "top_clips": engine.top_clips,
                "queue": clipper.state,
                "scout": engine.scout.snapshot(),
                "styles": STYLES,
                "settings": cfg,
                "keys": {"twitch": config.has_keys("twitch"), "kick": config.has_keys("kick")},
                "output_dir": str(config.output_dir()),
            })
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/discover":
                return self._json(engine.refresh_discovery())
            if u.path == "/api/scout/refresh":
                threading.Thread(target=engine.scout.refresh, daemon=True).start()
                return self._json({"ok": True})
            if u.path == "/api/scout/ignore":
                row = next((r for r in engine.scout.ranked if r["id"] == b["id"]), None)
                if not row:
                    raise ValueError("unknown streamer")
                cur = store.effective_rule(config.load(), row["key"])["record"]
                store.rule_set(row["key"], {"record": not cur}, name=row.get("display") or row["channel"],
                               platform=row["platform"], url=row.get("url"))
                return self._json({"record": not cur})
            if u.path == "/api/plan":
                engine.apply_plan(int(b["id"]), b.get("merge"), bool(b.get("reset")))
                return self._json({"ok": True})
            if u.path == "/api/ai-edit":
                text = (b.get("text") or "").strip()
                if not text:
                    raise ValueError("write how you want the clip edited")
                m = store.get_moment(int(b["id"]))
                if not m or not m.get("clip") or m.get("cleaned"):
                    raise ValueError("this clip has no video to edit")
                threading.Thread(target=ai.plan_from_request, args=(m["id"], text[:400]), daemon=True).start()
                return self._json({"ok": True})
            if u.path == "/api/ai-ideas":
                m = store.get_moment(int(b["id"]))
                if not m or not m.get("clip") or m.get("cleaned"):
                    raise ValueError("this clip has no video to look at")
                threading.Thread(target=ai.suggest, args=(m["id"],), daemon=True).start()
                return self._json({"ok": True})
            if u.path == "/api/rules":
                return self._json(set_rule(b))
            if u.path == "/api/running":
                on = bool(b.get("on"))
                config.save({"running": on})
                engine.set_running(on)
                return self._json({"running": on})
            if u.path == "/api/topclips":
                return self._json(engine.refresh_top_clips())
            if u.path == "/api/watch":
                return self._json({"id": engine.watch(b["url"], b.get("title", ""), b.get("display", ""))})
            if u.path == "/api/unwatch":
                engine.unwatch(b["id"])
                return self._json({"ok": True})
            if u.path == "/api/clip":
                return self._json({"moment": engine.clip_now(b["id"])})
            if u.path == "/api/grab":
                return self._json({"moment": engine.grab_top_clip(b)})
            if u.path == "/api/review":
                engine.review(int(b["id"]), b["action"], b.get("style"), b.get("hook"), b.get("length"),
                              bool(b.get("delete")))
                return self._json({"ok": True})
            if u.path == "/api/cleanup":
                return self._json(cleanup(b))
            if u.path == "/api/facecam":
                return self._json(set_facecam(b))
            if u.path == "/api/settings":
                return self._json(config.save(b))
            if u.path == "/api/open-folder":
                out = config.output_dir()
                out.mkdir(parents=True, exist_ok=True)
                from cw.osopen import reveal
                reveal(out)
                return self._json({"ok": True})
        except (ValueError, KeyError) as e:
            return self._json({"error": str(e)}, 400)
        self._json({"error": "not found"}, 404)


def _exit(*_):
    engine.shutdown()
    sys.exit(0)


def _watch_parent():
    """If whatever launched us goes away, stop recorders instead of leaving them running."""
    import os
    import time as _t
    parent = os.getppid()
    while True:
        _t.sleep(2)
        if os.getppid() != parent:
            engine.shutdown()
            os._exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)
    threading.Thread(target=_watch_parent, daemon=True).start()
    port = config.load()["port"]
    print(f"ClipWatch → http://127.0.0.1:{port}   (clips: {config.output_dir()})")
    print("Design & made by siposraulmihai.xyz")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
