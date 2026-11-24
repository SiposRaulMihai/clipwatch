"""Turns buffer chunks into clips. One job at a time (8 GB Mac)."""
import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from . import config, store
from .recorder import seg_time

jobs = queue.PriorityQueue()  # cutting clips (0) always before 9:16 edits (1): the buffer won't wait
_seq = __import__("itertools").count()
state = {"busy": None, "queued": 0, "edits_pending": 0}


def submit(job, prio=0):
    if job[0] == "vertical":
        state["edits_pending"] += 1
    jobs.put((prio, next(_seq), job))
    state["queued"] = jobs.qsize()


def _safe(s):
    return re.sub(r"[^\w.-]+", "-", s).strip("-")[:40] or "stream"


def _dest(platform, channel, tag, t):
    """GTA6_CLIPS/<streamer>/<date>/source/<file>: the 16:9 recording, thumbnail and details.
    The 9:16 edits land one folder up, next to source/ (see editor.edit_path)."""
    day = time.strftime("%Y-%m-%d", time.localtime(t))
    folder = config.output_dir() / _safe(channel) / day / "source"
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{platform}_{_safe(channel)}_{time.strftime('%H%M%S', time.localtime(t))}_{tag}"
    return folder / base


def _run(cmd, timeout=600):
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-400:])


def _thumb(video, jpg, at=None):
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if at:
        args += ["-ss", str(at)]
    _run(args + ["-i", str(video), "-frames:v", "1", "-vf", "scale=480:-2", str(jpg)], timeout=60)


def _gather(rec, t0, t1):
    """Buffered chunks covering t0..t1 (waits for the ones after the moment)."""
    deadline = time.time() + 90
    while time.time() < deadline:
        segs = rec.segments()
        if len(segs) > 1 and seg_time(segs[-2]) >= t1 - config.load()["segment_seconds"]:
            break
        time.sleep(2)
    parts = rec.segments_between(t0, t1)
    segs = rec.segments()
    if segs and parts and parts[-1] == segs[-1]:
        parts = parts[:-1]  # last chunk is still being written
    if not parts:
        raise RuntimeError("nothing buffered for that moment yet")
    return parts


def _concat(parts, out):
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", "concat:" + "|".join(str(p) for p in parts),
          "-c", "copy", "-movflags", "+faststart", str(out)])  # ffmpeg adds aac_adtstoasc itself when needed


def make_clip(mid, rec, t0, t1):
    m = store.get_moment(mid)
    parts = _gather(rec, t0, t1)
    base = _dest(m["platform"], m["channel"], m["tag"], m["at"])
    _concat(parts, base.with_suffix(".mp4"))
    post = config.load()["post_roll"] if m.get("kind") != "best" else config.load().get("best_post_roll", 30)
    _finish(mid, base, thumb_at=max(1, (t1 - t0) - post - 3))


def _finish(mid, base, thumb_at):
    m = store.get_moment(mid)
    mp4, jpg = base.with_suffix(".mp4"), base.with_suffix(".jpg")
    try:
        _thumb(mp4, jpg, at=thumb_at)
    except RuntimeError:
        jpg = None
    base.with_suffix(".json").write_text(json.dumps(
        {k: m[k] for k in ("platform", "channel", "title", "url", "score", "tag", "reasons", "chat")}
        | {"at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["at"]))},
        indent=2, ensure_ascii=False))
    store.update_moment(mid, clip=str(mp4), thumb=str(jpg) if jpg else None, status="ready", error=None)
    _auto_edit(mid)


def _auto_edit(mid):
    cfg = config.load()
    m = store.get_moment(mid)
    if not store.effective_rule(cfg, store.facecam_key(m["platform"], m["stream_id"], m["channel"]))["auto_edit"]:
        return  # this streamer's rule: no automatic 9:16
    try:
        low = config.disk_free_gb() < cfg.get("min_free_gb", 10)
    except OSError:
        low = False
    if low:
        store.update_moment(mid, edit_state="lowdisk")  # free space, then click Make 9:16
    else:
        queue_edit(mid, auto=True)


def make_vertical(mid, style=None, length=None):
    from . import editor  # imported lazily: needs numpy + Pillow from .venv
    store.update_moment(mid, edit_state="editing", error=None)
    try:
        editor.make_edit(mid, style, length)
        store.update_moment(mid, edit_state="done")
    except Exception:
        store.update_moment(mid, edit_state="failed")
        raise


def queue_edit(mid, style=None, auto=False, length=None):
    if auto and state["edits_pending"] >= config.load().get("max_edit_queue", 12):
        store.update_moment(mid, edit_state="skipped")  # backlog: click "Make 9:16" on the card later
        return False
    store.update_moment(mid, edit_state="queued")
    submit(("vertical", mid, style, length), prio=1)
    return True


def download_clip(mid, url):
    m = store.get_moment(mid)
    base = _dest(m["platform"], m["channel"], "topclip", m["at"])
    mp4 = base.with_suffix(".mp4")
    _run(["yt-dlp", "-q", "--no-warnings", "-f", "best[ext=mp4]/best", "--remux-video", "mp4",
          "-o", str(mp4), url], timeout=300)
    jpg = base.with_suffix(".jpg")
    try:
        _thumb(mp4, jpg, at=2)
    except RuntimeError:
        jpg = None
    store.update_moment(mid, clip=str(mp4), thumb=str(jpg) if jpg else None, status="ready")
    _auto_edit(mid)


def clip_files(m):
    """The big files of one clip: the 16:9 recording and every 9:16 edit (thumbnail + .json stay)."""
    from . import editor
    if not m.get("clip"):
        return []
    root = config.output_dir().resolve()
    clip = Path(m["clip"])
    cands = {clip} | {editor.edit_path(clip, k) for k in editor.STYLES} | {clip.with_name(f"{clip.stem}_{k}.mp4") for k in editor.STYLES}
    if m.get("vertical"):
        cands.add(Path(m["vertical"]))
    out = []
    for f in cands:
        try:
            if f.suffix == ".mp4" and f.is_file() and root in f.resolve().parents:
                out.append(f)
        except OSError:
            pass
    return sorted(out)


def delete_clip_files(mid):
    """Delete one clip's video files (after you posted it). Returns (files, bytes)."""
    m = store.get_moment(mid)
    if not m or m.get("cleaned"):
        return 0, 0
    n = freed = 0
    for f in clip_files(m):
        try:
            size = f.stat().st_size
            f.unlink()
            n += 1
            freed += size
        except OSError:
            pass
    store.update_moment(mid, cleaned=time.time())
    return n, freed


def migrate_layout():
    """Clips used to sit in GTA6_CLIPS/<date>/. Move each into GTA6_CLIPS/<streamer>/<date>/ (9:16 edits) and
    .../source/ (16:9 recording, thumbnail, details). Moves only, never overwrites; safe to run again."""
    from . import editor
    root = config.output_dir()
    moved = 0

    def mv(f, dest):
        nonlocal moved
        if f.exists() and not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            f.rename(dest)
            moved += 1
        return dest if dest.exists() else f

    for m in store.list_moments(100000):
        if not m.get("clip"):
            continue
        clip = Path(m["clip"])
        if clip.parent.name == "source" or root.resolve() not in clip.resolve().parents:
            continue
        src_dir = root / _safe(m["channel"]) / clip.parent.name / "source"
        upd = {"clip": str(mv(clip, src_dir / clip.name) if clip.exists() else src_dir / clip.name)}
        thumb = mv(clip.with_suffix(".jpg"), src_dir / (clip.stem + ".jpg"))
        if m.get("thumb"):
            upd["thumb"] = str(thumb)
        mv(clip.with_suffix(".json"), src_dir / (clip.stem + ".json"))
        for k in editor.STYLES:
            f = clip.with_name(f"{clip.stem}_{k}.mp4")
            d = mv(f, src_dir.parent / f.name)
            if m.get("vertical") and Path(m["vertical"]) == f:
                upd["vertical"] = str(d)
        store.update_moment(m["id"], **upd)
    for d in root.iterdir():  # old date folders that are now empty
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name):
            try:
                d.rmdir()
            except OSError:
                pass
    return moved


def _worker():
    while True:
        _, _, job = jobs.get()
        state["queued"] = jobs.qsize()
        kind, mid = job[0], job[1]
        state["busy"] = {"kind": kind, "moment": mid}
        try:
            if kind == "clip":
                make_clip(mid, *job[2:])
            elif kind == "vertical":
                make_vertical(mid, job[2] if len(job) > 2 else None, job[3] if len(job) > 3 else None)
            elif kind == "download":
                download_clip(mid, job[2])
        except Exception as e:  # noqa: BLE001 — keep the queue alive, show the error on the card
            store.update_moment(mid, error=f"{kind}: {e}"[:400], **({"status": "failed"} if kind != "vertical" else {}))
        finally:
            if kind == "vertical":
                state["edits_pending"] = max(0, state["edits_pending"] - 1)
            state["busy"] = None
            state["queued"] = jobs.qsize()


threading.Thread(target=_worker, daemon=True, name="clipper").start()
