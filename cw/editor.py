"""9:16 TikTok edits of a clip, in several gaming styles.

Pipeline: audio peak → trim around it → transcribe (word timestamps) → find the facecam (Apple Vision)
→ hook title (Codex, template fallback) → render text as PNGs (this ffmpeg has no text filters)
→ one ffmpeg pass: layout + punch-in zoom + shake + flash + captions + hook + credit + progress bar.
"""
import hashlib
import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import config, store

OW, OH, FPS = 1080, 1920, 30
ACCENT = (255, 90, 95)
YELLOW = (255, 225, 77)
GREEN = (34, 212, 107)
FONTS = {  # first font file that exists wins (macOS, Windows, Linux)
    "black": ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "C:/Windows/Fonts/ariblk.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"],
    "bold": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"],
    "impact": ["/System/Library/Fonts/Supplemental/Impact.ttf", "C:/Windows/Fonts/impact.ttf",
               "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}
EMOJI_FONT = next((f for f in ["/System/Library/Fonts/Apple Color Emoji.ttc",
                               "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
                               "/usr/share/fonts/noto/NotoColorEmoji.ttf"] if Path(f).exists()), "")
FACEDETECT_SRC = config.ROOT / "cw" / "native" / "facedetect.swift"
FACEDETECT = config.DATA / "bin" / "facedetect"
VENV_PY = config.ROOT / ".venv" / "bin" / "python"
WORK = config.DATA / "edit"

# ---------------------------------------------------------------- styles
STYLES = {
    "streamer": {"label": "Streamer stack", "about": "Facecam on top, gameplay below, yellow word-by-word captions",
                 "layout": "stack", "face_h": 700, "captions": "pop", "hook": "box", "shake": True, "flash": True, "bar": True},
    "reaction": {"label": "Big reaction", "about": "Huge facecam for reaction moments, green highlight captions",
                 "layout": "stack", "face_h": 1020, "captions": "box", "hook": "box", "shake": False, "flash": True, "bar": True},
    "bubble": {"label": "Facecam bubble", "about": "Full-screen gameplay with a round facecam bubble",
               "layout": "bubble", "captions": "pop", "hook": "top", "shake": True, "flash": False, "bar": True},
    "cinema": {"label": "Cinematic", "about": "Whole 16:9 frame on a blurred background, clean subtitles",
               "layout": "blur", "captions": "clean", "hook": "title", "shake": False, "flash": False, "bar": False},
    "meme": {"label": "Meme banner", "about": "White top banner with the joke, video below, Impact captions",
             "layout": "meme", "captions": "meme", "hook": "meme", "shake": True, "flash": True, "bar": False},
}
EMOJI = {"funny": "😭", "hype": "😳", "loud": "💀", "spike": "🔥", "manual": "🔥", "topclip": "😂"}
TEMPLATES = {
    "funny": ["{N} CAN'T STOP LAUGHING", "{N} LOST IT IN GTA 6", "GTA 6 HAD {N} IN TEARS"],
    "hype": ["{N} WAS NOT READY", "GTA 6 DID {N} DIRTY", "WAIT FOR {N}'S REACTION"],
    "loud": ["{N} SCREAMED AT GTA 6", "{N} RAGED SO HARD", "THIS MADE {N} LOSE IT"],
    "spike": ["CHAT WENT CRAZY FOR THIS", "WAIT FOR IT", "{N} IN GTA 6 IS INSANE"],
    "manual": ["WAIT FOR IT", "{N} IN GTA 6 IS INSANE"],
    "topclip": ["{N} IN GTA 6"],
}


# ---------------------------------------------------------------- small helpers
def _run(cmd, timeout=600, **kw):
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[-600:])
    return out


def probe(path):
    out = _run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                "-of", "json", str(path)], timeout=30)
    d = json.loads(out.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    if not v:
        raise RuntimeError("clip has no video")
    return {"w": int(v["width"]), "h": int(v["height"]), "dur": float(d["format"]["duration"]),
            "audio": any(s["codec_type"] == "audio" for s in d["streams"])}


_font_cache = {}


def font(name, size):
    key = (name, size)
    if key not in _font_cache:
        path = next((f for f in FONTS[name] if Path(f).exists()), None)
        _font_cache[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
    return _font_cache[key]


def even(v):
    return max(2, int(v) // 2 * 2)


# ---------------------------------------------------------------- audio
def audio_envelope(src, hop=0.05):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, timeout=120).stdout
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(16000 * hop)
    if len(a) < n * 4:
        return np.zeros(1), hop
    frames = a[: len(a) // n * n].reshape(-1, n)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-9)
    db = 20 * np.log10(rms + 1e-9)
    k = max(1, int(0.4 / hop))
    smooth = np.convolve(db, np.ones(k) / k, mode="same")
    return smooth, hop


def find_peak(env, hop, lo, hi):
    i0, i1 = max(0, int(lo / hop)), min(len(env), int(hi / hop))
    if i1 - i0 < 2:
        return (lo + hi) / 2
    return (i0 + int(np.argmax(env[i0:i1]))) * hop


def second_peak(env, hop, lo, hi, p1, min_gap=3.0):
    i0, i1 = max(0, int(lo / hop)), min(len(env), int(hi / hop))
    best, bt = -1e9, None
    for i in range(i0, i1):
        t = i * hop
        if abs(t - p1) >= min_gap and env[i] > best:
            best, bt = env[i], t
    peak_val = env[min(len(env) - 1, int(p1 / hop))]
    return bt if bt is not None and best >= peak_val - 4 else None


def extra_peaks(env, hop, lo, hi, taken, n, min_gap=6.0):
    """The next-loudest moments (up to n, min_gap apart) for extra zoom punches in long edits."""
    if n <= 0 or not taken:
        return []
    ref = env[min(len(env) - 1, int(taken[0] / hop))]
    i0, i1 = max(0, int(lo / hop)), min(len(env), int(hi / hop))
    out = []
    for _ in range(n):
        best, bt = -1e9, None
        for i in range(i0, i1):
            t = i * hop
            if env[i] > best and all(abs(t - q) >= min_gap for q in taken + out):
                best, bt = env[i], t
        if bt is None or best < ref - 6:
            break
        out.append(bt)
    return out


def choose_window(m, info, env, hop, cfg):
    dur = info["dur"]
    before, after = cfg.get("edit_before", 12), cfg.get("edit_after", 7)
    if m["tag"] == "topclip":
        p = find_peak(env, hop, 1, dur - 1)
        return 0.0, min(dur, 59.0), p
    if m["tag"] == "manual":
        p = find_peak(env, hop, 2, dur - 2)
    else:
        post = {"long": cfg.get("long_post_roll", 20), "best": cfg.get("best_post_roll", 30)}.get(m.get("kind"), cfg["post_roll"])
        spike_at = max(0.0, dur - post)
        p = find_peak(env, hop, max(0, spike_at - 16), min(dur, spike_at + 1.5))
    start = max(0.0, p - before)
    end = min(dur, p + after)
    if end - start < 14:
        start = max(0.0, end - 14)
        end = min(dur, start + 14)
    return start, end, p


# ---------------------------------------------------------------- transcription
def transcribe(src, start, length, work, cfg):
    wav = work / "speech.wav"
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src),
          "-ac", "1", "-ar", "16000", str(wav)], timeout=120)
    try:
        out = _run([str(VENV_PY), "-m", "cw.transcribe", str(wav), cfg.get("whisper_model", "small")],
                   timeout=300, cwd=str(config.ROOT))
        return json.loads(out.stdout.strip().splitlines()[-1])["words"]
    except (RuntimeError, ValueError, IndexError, subprocess.TimeoutExpired):
        return []


# ---------------------------------------------------------------- facecam
def _facedetect_bin():
    if not FACEDETECT.exists() or FACEDETECT.stat().st_mtime < FACEDETECT_SRC.stat().st_mtime:
        FACEDETECT.parent.mkdir(parents=True, exist_ok=True)
        _run(["swiftc", "-O", str(FACEDETECT_SRC), "-o", str(FACEDETECT)], timeout=300)
    return FACEDETECT


def sample_frames(src, start, length, work, n=8, width=960):
    files = []
    for i in range(n):
        t = start + (i + 0.5) * length / n
        f = work / f"probe_{i}.jpg"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
                            "-vf", f"scale={width}:-2", "-q:v", "3", str(f)], capture_output=True, timeout=60)
        if r.returncode == 0 and f.exists():
            files.append(f)
    return files


def detect_face_cluster(files, within=None):
    """Faces that stay in the same place across frames = the webcam (game faces move and cut).

    within: a facecam box you drew (normalized) — only faces inside it count, and fewer hits are enough.
    """
    if not files:
        return None
    try:
        out = _run([str(_facedetect_bin()), *map(str, files)], timeout=120)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return None  # no Apple Vision here (not macOS / no Xcode tools): layouts without a facecam crop
    res = json.loads(out.stdout)
    clusters = []
    for fi, r in enumerate(res):
        for x, y, w, h, conf in r["faces"]:
            if conf < 0.5 or h < (0.02 if within else 0.035):
                continue
            cx, cy = x + w / 2, y + h / 2
            if within and not (within["x"] <= cx <= within["x"] + within["w"]
                               and within["y"] <= cy <= within["y"] + within["h"]):
                continue
            for c in clusters:
                mx, my, mw = c["cx"], c["cy"], c["w"]
                if abs(cx - mx) < 0.05 and abs(cy - my) < 0.06 and 0.6 < w / mw < 1.6:
                    c["boxes"].append((fi, x, y, w, h))
                    c["frames"].add(fi)
                    c["cx"], c["cy"], c["w"] = (mx * 3 + cx) / 4, (my * 3 + cy) / 4, (mw * 3 + w) / 4
                    break
            else:
                clusters.append({"cx": cx, "cy": cy, "w": w, "boxes": [(fi, x, y, w, h)], "frames": {fi}})
    if not clusters:
        return None
    best = max(clusters, key=lambda c: (len(c["frames"]), -np.std([b[1] for b in c["boxes"]])))
    if len(best["frames"]) < (2 if within else max(3, len(files) // 2)):
        return None
    bx = np.median(np.array([b[1:] for b in best["boxes"]]), axis=0)
    return {"x": float(bx[0]), "y": float(bx[1]), "w": float(bx[2]), "h": float(bx[3]),
            "hits": len(best["frames"]), "of": len(files)}


def static_edges(files):
    """Edges present in (almost) every sampled frame: overlay borders like the webcam frame."""
    gx, gy = [], []
    for f in files:
        g = np.asarray(Image.open(f).convert("L").resize((480, 270)), dtype=np.float32)
        gx.append(np.abs(np.diff(g, axis=1)))
        gy.append(np.abs(np.diff(g, axis=0)))
    return np.percentile(np.stack(gx), 25, axis=0), np.percentile(np.stack(gy), 25, axis=0)


def _snap(face, files, info):
    """Webcam border lines around the face, from edges that stay put in every frame (normalized coords).

    A border must be a long straight line (most pixels along it are edges), and sit clearly outside the
    face, so the streamer's own outline or headset isn't mistaken for the webcam frame.
    """
    try:
        sx, sy = static_edges(files)
    except (OSError, ValueError):
        return None
    W, H = 480, 270
    fw, fh = face["w"] * W, face["h"] * H
    fx0, fy0 = face["x"] * W, face["y"] * H
    fx1, fy1 = fx0 + fw, fy0 + fh
    thr = max(14.0, float(np.median(sx)) * 3.5)
    span_y = slice(int(max(0, fy0 - fh * 0.5)), int(min(H - 1, fy1 + fh * 1.5)))
    span_x = slice(int(max(0, fx0 - fw * 1.2)), int(min(W - 2, fx1 + fw * 1.2)))

    def straight_v(x):
        return float((sx[span_y, x] > thr).mean())

    def straight_h(y):
        return float((sy[y, span_x] > thr).mean())

    def first(score, idxs):
        for i in idxs:
            if 0 <= i < (W - 1 if score is straight_v else H - 1) and score(i) >= 0.6:
                return i
        return None

    right = first(straight_v, range(int(fx1 + fw * 0.3), int(min(W - 1, fx1 + fw * 6))))
    left = first(straight_v, range(int(fx0 - fw * 0.3), int(max(-1, fx0 - fw * 6)), -1))
    bottom = first(straight_h, range(int(fy1 + fh * 0.12), int(min(H - 1, fy1 + fh * 5))))
    top = first(straight_h, range(int(fy0 - fh * 0.12), int(max(-1, fy0 - fh * 3)), -1))
    if bottom is None:
        # no straight line under the face: an overlay panel is perfectly still, the streamer's body moves
        try:
            tsd = np.std(np.stack([np.asarray(Image.open(f).convert("L").resize((W, H)), dtype=np.float32)
                                   for f in files]), axis=0)
            xs = slice(int(max(0, fx0 - fw * 0.3)), int(min(W, fx1 + fw * 0.3)))
            run = 0
            for y in range(int(fy1 + fh * 0.12), int(min(H - 1, fy1 + fh * 4))):
                if float(tsd[y, xs].mean()) < 2.5:
                    run += 1
                    if run >= 4:
                        bottom = y - 3
                        break
                else:
                    run = 0
        except (OSError, ValueError):
            pass
    edges = {
        "L": (left + 1) / W if left is not None else None,
        "R": (right + 1) / W if right is not None else None,
        "T": (top + 1) / H if top is not None else None,
        "B": (bottom + 1) / H if bottom is not None else None,
    }
    return edges if any(v is not None for v in edges.values()) else None


def face_crop(face, cam, info, aspect, tight=False):
    """A crop (source px) of the given aspect around the streamer's face, kept inside any webcam edges found."""
    W, H = info["w"], info["h"]
    fw, fh = face["w"] * W, face["h"] * H
    cx, cy = (face["x"] + face["w"] / 2) * W, (face["y"] + face["h"] / 2) * H
    big = fw > 0.18 * W  # camera-first stream: the face fills the frame
    e = cam or {}
    found = sum(v is not None for v in e.values())
    # no webcam border found: frame head + shoulders (a wide guess pulls in overlays and alerts)
    w_max = min(W, fw * (2.6 if tight else 2.8 if big else 5.2 if found >= 2 else 3.6))
    h_max = min(H, fh * (2.4 if tight else 2.4 if big else 3.6 if found >= 2 else 2.6))
    # webcam edges (static lines around the face) limit the area further
    lo_x = e["L"] * W if e.get("L") is not None else 0.0
    hi_x = e["R"] * W if e.get("R") is not None else float(W)
    lo_y = e["T"] * H if e.get("T") is not None else 0.0
    hi_y = e["B"] * H if e.get("B") is not None else float(H)
    if not tight:
        w_max = min(w_max, hi_x - lo_x) if e.get("L") is not None and e.get("R") is not None else w_max
        h_max = min(h_max, hi_y - lo_y) if e.get("T") is not None and e.get("B") is not None else h_max
    w, h = (w_max, w_max / aspect) if w_max / aspect <= h_max else (h_max * aspect, h_max)
    x, y = cx - w / 2, cy - h * 0.42
    # keep inside the found edges: shift first, shrink (keeping the aspect) only if it still doesn't fit
    if not tight:
        x = min(max(x, lo_x), hi_x - w)
        y = min(max(y, lo_y), hi_y - h)
        if x < lo_x or y < lo_y:
            sw = min(w, hi_x - lo_x)
            sh = min(h, hi_y - lo_y)
            w, h = (sw, sw / aspect) if sw / aspect <= sh else (sh * aspect, sh)
            x = min(max(cx - w / 2, lo_x), hi_x - w)
            y = min(max(cy - h * 0.42, lo_y), hi_y - h)
    x = min(max(0, x), W - w)
    y = min(max(0, y), H - h)
    return even(x), even(y), even(w), even(h)


def box_crop(box, face, info, aspect):
    """The biggest crop of this aspect that stays inside the facecam box you drew, centred on the face when
    one is seen in it (else on the box)."""
    W, H = info["w"], info["h"]
    bx, by, bw, bh = box["x"] * W, box["y"] * H, box["w"] * W, box["h"] * H
    w, h = (bw, bw / aspect) if bw / aspect <= bh else (bh * aspect, bh)
    if face:
        cx, cy = (face["x"] + face["w"] / 2) * W, (face["y"] + face["h"] / 2) * H
    else:
        cx, cy = bx + bw / 2, by + bh * 0.42
    x = min(max(cx - w / 2, bx), bx + bw - w)
    y = min(max(cy - h * 0.42, by), by + bh - h)
    x = min(max(0, x), W - w)
    y = min(max(0, y), H - h)
    return even(x), even(y), even(min(w, W)), even(min(h, H))


def center_crop(info, aspect, bias_y=0.5):
    W, H = info["w"], info["h"]
    if W / H > aspect:
        h, w = H, H * aspect
    else:
        w, h = W, W / aspect
    return even((W - w) / 2), even((H - h) * bias_y), even(w), even(h)


def tighter(crop, info, z=1.22):
    x, y, w, h = crop
    nw, nh = even(w / z), even(h / z)
    nx = min(max(0, x + (w - nw) / 2), info["w"] - nw)
    ny = min(max(0, y + (h - nh) / 2), info["h"] - nh)
    return even(nx), even(ny), nw, nh


# ---------------------------------------------------------------- text → PNG
def clean_word(w):
    w = re.sub(r"[^\w'?!%$-]", "", w)
    return w


def chunk_words(words, max_words, max_chars, gap=0.45):
    chunks, cur = [], []
    for w in words:
        t = clean_word(w["w"])
        if not t:
            continue
        item = {**w, "t": t}
        if cur:
            chars = sum(len(c["t"]) + 1 for c in cur) + len(t)
            if (len(cur) >= max_words or chars > max_chars or w["s"] - cur[-1]["e"] > gap
                    or cur[-1]["w"][-1:] in ".?!"):
                chunks.append(cur)
                cur = []
        cur.append(item)
    if cur:
        chunks.append(cur)
    return chunks


CAPTION_STYLES = {
    #        font     size upper  stroke  max_words max_chars  highlight
    "pop":   ("black", 86, True, 10, 3, 16, "yellow"),
    "box":   ("black", 80, True, 8, 3, 16, "box"),
    "clean": ("bold", 58, False, 0, 7, 34, "none"),
    "meme":  ("impact", 92, True, 7, 5, 26, "none"),
}


def _layout_lines(draw, tokens, f, max_w):
    space = draw.textlength(" ", font=f)
    lines, cur, cur_w = [], [], 0
    for i, t in enumerate(tokens):
        tw = draw.textlength(t, font=f)
        if cur and cur_w + space + tw > max_w:
            lines.append(cur)
            cur, cur_w = [], 0
        cur.append((i, t, tw))
        cur_w += (space if len(cur) > 1 else 0) + tw
    if cur:
        lines.append(cur)
    return lines, space


def caption_png(tokens, active, style, path, max_w=940):
    fname, size, upper, stroke, _, _, hl = CAPTION_STYLES[style]
    tokens = [t.upper() if upper else t for t in tokens]
    canvas = Image.new("RGBA", (OW, 360), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    for s in (size, int(size * 0.88), int(size * 0.76)):
        f = font(fname, s)
        lines, space = _layout_lines(d, tokens, f, max_w)
        if len(lines) <= 2:
            break
    line_h = int(s * 1.18)
    total_h = line_h * len(lines)
    y = (360 - total_h) // 2
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    pad = int(s * 0.14)
    for line in lines:
        lw = sum(tw for _, _, tw in line) + space * (len(line) - 1)
        x = (OW - lw) / 2
        for i, t, tw in line:
            is_active = i == active
            if hl == "box" and is_active:
                d.rounded_rectangle([x - pad, y - pad * 0.4, x + tw + pad, y + s * 1.05 + pad * 0.4],
                                    radius=int(s * 0.22), fill=GREEN + (255,))
            fill = (255, 255, 255, 255)
            if hl == "yellow" and is_active:
                fill = YELLOW + (255,)
            if style == "clean":
                sd.text((x, y + 4), t, font=f, fill=(0, 0, 0, 200))
            else:
                sd.text((x, y + 7), t, font=f, fill=(0, 0, 0, 170), stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
            d.text((x, y), t, font=f, fill=fill,
                   stroke_width=0 if (hl == "box" and is_active) or style == "clean" else stroke,
                   stroke_fill=(0, 0, 0, 255))
            x += tw + space
        y += line_h
    shadow = shadow.filter(ImageFilter.GaussianBlur(6 if style != "clean" else 3))
    out = Image.alpha_composite(shadow, canvas)
    out.save(path)


def build_captions(words, style, length, work, cfg_censor=True):
    """PNG per word state + an ffconcat timeline. Returns the concat path or None."""
    _, _, _, _, max_words, max_chars, hl = CAPTION_STYLES[style]
    chunks = chunk_words(words, max_words, max_chars)
    if not chunks:
        return None
    blank = work / "cap_blank.png"
    Image.new("RGBA", (OW, 360), (0, 0, 0, 0)).save(blank)
    events = []
    n = 0
    for ci, ch in enumerate(chunks):
        next_start = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else length
        chunk_end = min(next_start, ch[-1]["e"] + 0.3)
        tokens = [censor(w["t"]) if cfg_censor else w["t"] for w in ch]
        states = range(len(ch)) if hl != "none" else [None]
        for k, wi in enumerate(states):
            png = work / f"cap_{n:04d}.png"
            n += 1
            caption_png(tokens, wi, style, png)
            if wi is None:
                s, e = ch[0]["s"], chunk_end
            else:
                s = ch[wi]["s"] if wi else ch[0]["s"]
                e = ch[wi + 1]["s"] if wi + 1 < len(ch) else chunk_end
            events.append((max(0.0, s), min(length, max(e, s + 0.1)), png))
    lines = ["ffconcat version 1.0"]
    t = 0.0
    for s, e, png in events:
        s = max(s, t)
        if e - s < 1 / FPS:
            continue
        if s - t >= 1 / FPS:
            lines += [f"file '{blank}'", f"duration {s - t:.3f}"]
        lines += [f"file '{png}'", f"duration {e - s:.3f}"]
        t = e
    if length - t > 0:
        lines += [f"file '{blank}'", f"duration {length - t:.3f}"]
    lines.append(f"file '{blank}'")
    path = work / "captions.ffconcat"
    path.write_text("\n".join(lines) + "\n")
    return path


def emoji_png(ch, size):
    try:
        f = ImageFont.truetype(EMOJI_FONT, 109 if "Noto" in EMOJI_FONT else 160)
        im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((20, 20), ch, font=f, embedded_color=True)
        bbox = im.getbbox()
        if not bbox:
            return None
        return im.crop(bbox).resize((size, size), Image.LANCZOS)
    except (OSError, ValueError):
        return None


def hook_png(text, emoji, style, path):
    """Returns (png path, height). 'meme' draws the whole white top banner."""
    text = text.upper()
    if style == "meme":
        banner_h = 520
        im = Image.new("RGBA", (OW, banner_h), (255, 255, 255, 255))
        d = ImageDraw.Draw(im)
        for s in (78, 68, 60, 52):
            f = font("black", s)
            lines, space = _layout_lines(d, text.split(), f, 960)
            if len(lines) <= 3:
                break
        lh = int(s * 1.2)
        y = (banner_h - lh * len(lines)) / 2
        for line in lines:
            lt = " ".join(t for _, t, _ in line)
            d.text(((OW - d.textlength(lt, font=f)) / 2, y), lt, font=f, fill=(10, 10, 12, 255))
            y += lh
        im.save(path)
        return path, banner_h
    boxed = style in ("box", "top")
    size = 64 if boxed else 74
    probe_im = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(probe_im)
    for s in (size, int(size * 0.88), int(size * 0.78)):
        f = font("black", s)
        lines, space = _layout_lines(d, text.split(), f, 860)
        if len(lines) <= 2:
            break
    lh = int(s * 1.22)
    pad_x, pad_y = 40, 26
    text_w = max(sum(tw for _, _, tw in ln) + space * (len(ln) - 1) for ln in lines)
    box_w, box_h = int(text_w + pad_x * 2), int(lh * len(lines) + pad_y * 2)
    H = box_h + 90
    im = Image.new("RGBA", (OW, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    bx, by = (OW - box_w) // 2, 60
    if boxed:
        sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([bx, by + 10, bx + box_w, by + box_h + 10], radius=28, fill=(0, 0, 0, 120))
        im = Image.alpha_composite(im, sh.filter(ImageFilter.GaussianBlur(10)))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([bx, by, bx + box_w, by + box_h], radius=28, fill=(255, 255, 255, 255))
    y = by + pad_y
    for line in lines:
        lt = " ".join(t for _, t, _ in line)
        x = (OW - d.textlength(lt, font=f)) / 2
        if boxed:
            d.text((x, y), lt, font=f, fill=(12, 12, 14, 255))
        else:
            d.text((x, y), lt, font=f, fill=(255, 255, 255, 255), stroke_width=8, stroke_fill=(0, 0, 0, 255))
        y += lh
    e = emoji_png(emoji, 110) if emoji else None
    if e:
        e = e.rotate(-12, resample=Image.BICUBIC, expand=True)
        im.alpha_composite(e, (min(OW - e.width - 6, bx + box_w - 60), max(0, by - 52)))
    im.save(path)
    return path, H


def credit_png(m, path):
    name = m["channel"]
    label = {"twitch": f"twitch.tv/{name}", "kick": f"kick.com/{name}"}.get(m["platform"], f"{name} on YouTube")
    f = font("bold", 30)
    probe_im = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    tw = probe_im.textlength(label, font=f)
    w, h = int(tw + 56), 54
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=27, fill=(0, 0, 0, 150))
    dot = {"twitch": (169, 112, 255), "kick": (83, 252, 24)}.get(m["platform"], (255, 61, 61))
    d.ellipse([18, 21, 30, 33], fill=dot + (255,))
    d.text((40, 10), label, font=f, fill=(255, 255, 255, 255))
    im.save(path)
    return path, w, h


def circle_assets(d, work):
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, d - 1, d - 1], fill=255)
    mask_p = work / "bubble_mask.png"
    mask.save(mask_p)
    ring = Image.new("RGBA", (d + 24, d + 24), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse([2, 8, d + 22, d + 22], fill=(0, 0, 0, 90))
    ring = ring.filter(ImageFilter.GaussianBlur(6))
    rd = ImageDraw.Draw(ring)
    rd.ellipse([6, 6, d + 17, d + 17], outline=(255, 255, 255, 255), width=9)
    ring_p = work / "bubble_ring.png"
    ring.save(ring_p)
    return mask_p, ring_p


# ---------------------------------------------------------------- hook text
def _sanitize_hook(s):
    s = s.strip().splitlines()[-1] if s.strip() else ""
    s = re.sub(r"[\"“”#*:]", "", s)
    s = re.sub(r"[^\w\s'?!.,\u201c\u201d-]", "", s).strip().upper()
    words = s.split()
    return " ".join(words[:8]) if 2 <= len(words) <= 9 and len(s) <= 48 else ""


CENSOR = re.compile(r"\b(n+i+g+(?:g+|e+)r*s?|n+i+g+a+s?|f+u+c+k+\w*|c+u+n+t+s?|f+a+g+\w*|r+e+t+a+r+d+\w*)\b", re.I)


def censor(text):
    return CENSOR.sub(lambda mo: mo.group(0)[0] + "*" * (len(mo.group(0)) - 1), text)


ENERGY = {"bro", "nah", "no", "what", "why", "how", "yo", "wait", "oh", "bruh", "dude", "stop", "help", "run", "dead"}


def quote_near_peak(words, p_rel):
    """The streamer's own line around the moment, as a quote hook (popular on gaming TikTok)."""
    sents, cur = [], []
    for w in words:
        if cur and (w["s"] - cur[-1]["e"] > 0.6 or cur[-1]["w"][-1:] in ".?!"):
            sents.append(cur)
            cur = []
        cur.append(w)
    if cur:
        sents.append(cur)
    best, best_score = None, 0.0
    for snt in sents:
        toks = [clean_word(w["w"]) for w in snt]
        toks = [t for t in toks if t]
        if not 3 <= len(toks) <= 9:
            continue
        probs = [w.get("p", 0.6) for w in snt]
        # garbled transcript lines make terrible hooks: stray letters, low-confidence words
        if min(probs) < 0.45 or sum(probs) / len(probs) < 0.75:
            continue
        if any(len(t) == 1 and t.lower() not in ("i", "a") for t in toks):
            continue
        mid = (snt[0]["s"] + snt[-1]["e"]) / 2
        near = max(0.0, 1 - abs(mid - p_rel) / 6)
        if near <= 0:
            continue
        punch = sum(t.lower() in ENERGY for t in toks) * 0.4 + (snt[-1]["w"][-1:] in "?!") * 0.6
        if punch < 0.4:
            continue  # flat lines ("that was a great mission") make weak hooks
        conf = sum(w.get("p", 0.6) for w in snt) / len(snt)
        sc = near * 2 + punch + conf
        if sc > best_score:
            best, best_score = toks, sc
    if not best:
        return ""
    return "\u201c" + " ".join(best).upper() + "\u201d"


_codex_off_until = 0.0


def hook_text(m, words, cfg, p_rel):
    global _codex_off_until
    if m.get("hook") and (m.get("edit_info") or {}).get("hook_locked"):
        return m["hook"]  # typed by you in the dashboard
    name = re.sub(r"[^\w ]", "", m["channel"]).upper() or "HE"
    if m["tag"] == "topclip" and m.get("title"):
        t = _sanitize_hook(m["title"])
        if t:
            return censor(t)
    if cfg.get("hook_writer", "local") == "codex" and shutil.which("codex") and time.time() > _codex_off_until:
        said = " ".join(w["w"] for w in words)[:400]
        chat = ", ".join(c["text"] for c in (m.get("chat") or [])[:12])[:300]
        prompt = (
            "You write TikTok hook titles for gaming clips. "
            f"Clip: {m['channel']} streaming GTA 6 on {m['platform']}. Moment type: {m['tag']}. "
            f"Chat reaction: {chat or 'n/a'}. What the streamer says: \"{said or 'n/a'}\". "
            "Write ONE hook title, 3 to 7 words, ALL CAPS, that makes people stay to see what happens. "
            "You may use the streamer's name. No emojis, no hashtags, no quotes, no colons. "
            "Don't claim anything the transcript and chat don't support. Reply with only the title."
        )
        try:
            out = subprocess.run(["codex", "exec", "-m", cfg.get("codex_model", "gpt-6-luna"), "--sandbox", "read-only",
                                  "--skip-git-repo-check", prompt],
                                 capture_output=True, text=True, timeout=150, cwd=str(WORK))
            t = _sanitize_hook(out.stdout) if out.returncode == 0 else ""
            if t:
                return censor(t)
            _codex_off_until = time.time() + 3600  # usage limit or error: stop asking for an hour
        except (subprocess.TimeoutExpired, OSError):
            _codex_off_until = time.time() + 3600
    q = quote_near_peak(words, p_rel)
    if q:
        return censor(q)
    pool = TEMPLATES.get(m["tag"], TEMPLATES["spike"])
    pick = int(hashlib.md5(str(m["id"]).encode()).hexdigest(), 16) % len(pool)
    return pool[pick].replace("{N}", name)


# ---------------------------------------------------------------- the render
class Graph:
    def __init__(self, length):
        self.inputs = []      # extra inputs after the clip (index 0)
        self.chains = []
        self.length = length
        self.n = 0

    def add_input(self, args):
        self.inputs.append(args)
        return len(self.inputs)

    def image(self, path, loop_len=None):
        return self.add_input(["-loop", "1", "-framerate", str(FPS), "-t", f"{loop_len or self.length:.3f}", "-i", str(path)])

    def label(self, p="x"):
        self.n += 1
        return f"{p}{self.n}"


def active_intervals(words, env, hop, start, length, peak, gap=0.6, edges=True):
    """Speech, loud game moments and the peak as merged intervals (seconds from start)."""
    act = [(w["s"] - 0.12, w["e"] + 0.22) for w in words]
    i0, i1 = int(start / hop), int((start + length) / hop)
    seg_env = env[i0:i1]
    if len(seg_env):
        thr = float(np.median(seg_env)) + 5.0
        i = 0
        while i < len(seg_env):
            if seg_env[i] > thr:
                j = i
                while j < len(seg_env) and seg_env[j] > thr - 2:
                    j += 1
                act.append((i * hop - 0.25, j * hop + 0.3))
                i = j
            else:
                i += 1
    p = peak - start
    act.append((p - 2.5, p + 1.5))
    if edges:
        act.append((0.0, 1.0))
    act = sorted((max(0.0, a), min(length, b)) for a, b in act if b > a)
    merged = []
    for a, b in act:
        if merged and a - merged[-1][1] < gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return [(a, b) for a, b in merged if b - a >= 0.4]


def keep_segments(words, env, hop, start, length, peak, cfg):
    """Jump cuts: keep speech, loud game moments and the peak; drop the dead air between them."""
    full = [(0.0, length)]
    if not cfg.get("jump_cuts", True):
        return full
    merged = active_intervals(words, env, hop, start, length, peak, cfg.get("cut_min_gap", 0.6))
    kept = sum(b - a for a, b in merged)
    if not merged or kept < 8 or kept < length * 0.5 or length - kept < 0.6:
        return full  # mostly music/no speech, or nothing worth cutting
    return merged


def long_window(words, env, hop, dur, peak, cfg, force=False):
    """A 30–45 s highlight: grow from the peak through the action (speech or loud game audio), the lead-up
    first, until it calms down for a while or the cut edit would pass long_max. None = too little going on."""
    lo_len, hi_len = cfg.get("long_min", 30), cfg.get("long_max", 45)
    ivs = active_intervals(words, env, hop, 0.0, dur, peak, cfg.get("cut_min_gap", 0.6), edges=False)
    if not ivs:
        return None

    def dist(iv):
        a, b = iv
        return 0.0 if a <= peak <= b else min(abs(a - peak), abs(b - peak))

    k = min(range(len(ivs)), key=lambda i: dist(ivs[i]))
    lo = hi = k
    kept = ivs[k][1] - ivs[k][0]
    max_gap, last = 4.0, None  # a longer pause ends the scene in that direction
    while kept < hi_len:
        can_l = lo > 0 and ivs[lo][0] - ivs[lo - 1][1] <= max_gap
        can_r = hi < len(ivs) - 1 and ivs[hi + 1][0] - ivs[hi][1] <= max_gap
        if not (can_l or can_r):
            if kept < lo_len and max_gap < 9:  # quiet stretch: allow longer pauses to reach the minimum
                max_gap += 2.5
                continue
            break
        before = sum(min(b, peak) - a for a, b in ivs[lo:hi + 1] if a < peak)
        if can_l and (not can_r or before < 1.8 * (kept - before)):  # about 2/3 lead-up, 1/3 aftermath
            lo -= 1
            kept += ivs[lo][1] - ivs[lo][0]
            last = "L"
        else:
            hi += 1
            kept += ivs[hi][1] - ivs[hi][0]
            last = "R"
    if kept < lo_len * 0.8 and not force:
        return None
    start, end = max(0.0, ivs[lo][0] - 0.3), min(dur, ivs[hi][1] + 0.3)
    over = kept - hi_len
    if over > 1.0:  # the last piece overshoots: trim that side, never into the peak
        if last == "L":
            start = min(start + over, max(0.0, peak - 3.0))
        else:
            end = max(end - over, min(dur, peak + 2.0))
    if force and end - start < lo_len:  # you asked for long: pad with the lead-up
        start = max(0.0, min(start, peak - 0.65 * lo_len))
        end = min(dur, max(end, start + lo_len))
        start = max(0.0, min(start, end - lo_len))
    return start, end


def make_remap(segs, replay=None):
    """Source time (s from the window start) -> edit time. A slow-motion replay (piece index, from, to, speed) is
    inserted right after the piece that holds it. Returns remap, total length, the pieces' output spans and the
    replay's output span."""
    table, t, rspan = [], 0.0, None
    for i, (a, b) in enumerate(segs):
        table.append((a, b, t))
        t += b - a
        if replay and replay[0] == i:
            d = (replay[2] - replay[1]) / replay[3]
            rspan = (t, t + d)
            t += d

    def remap(x):
        for a, b, c in table:
            if x < a:
                return c
            if x <= b:
                return c + (x - a)
        return t

    return remap, t, [(c, c + (b - a)) for a, b, c in table], rspan


def subtract(segs, cuts):
    """Take the given ranges (seconds from the window start) out of the kept pieces."""
    out = []
    for a, b in segs:
        pieces = [(a, b)]
        for c0, c1 in cuts:
            nxt = []
            for x, y in pieces:
                if c1 <= x or c0 >= y:
                    nxt.append((x, y))
                else:
                    if c0 > x:
                        nxt.append((x, c0))
                    if c1 < y:
                        nxt.append((c1, y))
            pieces = nxt
        out += [(x, y) for x, y in pieces if y - x >= 0.3]
    return out


def render(m, style_key, src, info, start, length, peak, words, face, cam, hook, work, cfg, out, segs=None, box=None):
    st = {**STYLES[style_key], **(m.get("_st") or {})}  # an edit plan can change e.g. the caption look
    layout = st["layout"]
    if layout in ("stack", "bubble") and not face and not box:
        layout = "blur" if layout == "stack" else "full"
    cam_crop = None  # the facecam crop actually used (source px), stored so the dashboard can show it
    segs = segs or [(0.0, length)]
    replay = None
    if m.get("_replay"):  # slow-motion instant replay of one moment, right after it happens
        rt, speed = m["_replay"][0] - start, m["_replay"][1]
        ra, rb = max(0.0, rt - 1.8), min(length, rt + 1.2)
        idx = next((i for i, (a, b) in enumerate(segs) if a <= rt <= b), None)
        if idx is None:
            before = [i for i, (a, b) in enumerate(segs) if b <= rt]
            idx = before[-1] if before else 0
        if rb - ra >= 0.6:
            replay = (idx, ra, rb, speed)
    remap, L, new_segs, rspan = make_remap(segs, replay)
    cut = segs != [(0.0, length)] or replay is not None
    g = Graph(L)
    words = [{**w, "s": remap(w["s"]), "e": max(remap(w["s"]) + 0.05, remap(w["e"]))} for w in words]

    p1 = remap(peak - start)
    zooms = []
    if 0.4 < p1 < L - 0.8:
        zooms.append((max(0.0, p1 - 0.05), min(L, p1 + 1.6)))
    p2 = m.get("_p2")
    if p2 is not None:
        p2n = remap(p2 - start)
        if 0.4 < p2n < L - 0.8 and abs(p2n - p1) > 2:
            zooms.append((p2n, min(L, p2n + 1.1)))
    for px in m.get("_peaks") or []:  # long edits: a punch on each big moment, not just the peak
        pn = remap(px - start)
        if 0.4 < pn < L - 0.8 and all(abs(pn - a) > 2 for a, _ in zooms):
            zooms.append((pn, min(L, pn + 0.9)))
    if rspan:
        zooms.append(rspan)  # the replay plays zoomed in
    zen = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in zooms) or "0"
    shake = "0"
    if st["shake"] and zooms:
        a = zooms[0][0]
        shake = f"if(between(t,{a:.3f},{a + 0.35:.3f}),14*sin(90*t)*(1-(t-{a:.3f})/0.35),0)"
    # jump-cut zoom pulse: every second piece is pushed in a little, so cuts read as a style, not a glitch
    pulses = [ns for i, ns in enumerate(new_segs) if i % 2 == 1] if cut and cfg.get("cut_pulse", True) else []
    pen = "+".join(f"between(t,{a:.3f},{b - 0.001:.3f})" for a, b in pulses) or "0"

    # ---- layers: (kind, crop, size, pos, branches) — branches: list of (zoom factor, enable expr, shake expr)
    punch = [(1.22, zen, shake)] if zooms else []
    pulse = [(1.1, pen, "0")] if pulses else []
    layers = []
    if layout == "stack":
        fh = st["face_h"]
        if box:
            # the panel follows the shape you drew (within limits), so little of the box is cut away
            want = OW * (box["h"] * info["h"]) / max(1.0, box["w"] * info["w"])
            fh = even(min(max(want, fh * 0.8), min(fh * 1.25, 1100)))
        gh = OH - fh
        fcrop = box_crop(box, face, info, OW / fh) if box else face_crop(face, cam, info, OW / fh)
        cam_crop = fcrop
        gcrop = center_crop(info, OW / gh)
        layers += [("crop", fcrop, (OW, fh), (0, 0), pulse), ("crop", gcrop, (OW, gh), (0, fh), punch)]
        cap_y = fh + int(gh * (0.36 if fh < 900 else 0.30)) - 180
        hook_center = fh
        credit_xy = (24, 170)
        sticker_xy = (OW - 236, fh + 36)
        sep = fh
    elif layout in ("bubble", "full"):
        gcrop = center_crop(info, OW / OH)
        layers.append(("crop", gcrop, (OW, OH), (0, 0), pulse + punch))
        if layout == "bubble":
            bd = 380
            cam_crop = box_crop(box, face, info, 1.0) if box else face_crop(face, cam, info, 1.0, tight=True)
            layers.append(("bubble", cam_crop, (bd, bd), (44, 1110), []))
        cap_y = 880 - 180
        hook_center = 330
        credit_xy = (24, 170)
        sticker_xy = (OW - 236, 470)
        sep = None
    else:  # blur / meme: the whole 16:9 frame (it already contains the streamer's facecam)
        vw = OW
        vh = even(OW * info["h"] / info["w"])
        top = 520 if layout == "meme" else (OH - vh) // 2
        layers += [("blurbg", None, (OW, OH), (0, 0), []),
                   ("crop", (0, 0, info["w"], info["h"]), (vw, vh), (0, top), pulse + punch)]
        cap_y = top + vh + (40 if layout == "meme" else 30)
        hook_center = top - 150 if layout != "meme" else None
        credit_xy = (24, top + 20)
        sticker_xy = (OW - 236, top + 24)
        sep = None

    # ---- source: jump-cut pieces joined back together (video + audio with tiny fades, no clicks)
    if cut:
        k = len(segs)
        g.chains.append(f"[0:v]fps={FPS},setpts=PTS-STARTPTS,split={k}" + "".join(f"[r{i}]" for i in range(k)))
        for i, (a, b) in enumerate(segs):
            g.chains.append(f"[r{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        if info["audio"]:
            g.chains.append("[0:a]asetpts=PTS-STARTPTS,asplit=" + str(k) + "".join(f"[q{i}]" for i in range(k)))
            for i, (a, b) in enumerate(segs):
                d = b - a
                g.chains.append(f"[q{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                                f"afade=t=in:d=0.015,afade=t=out:st={max(0.0, d - 0.025):.3f}:d=0.025[a{i}]")
        seq = []
        for i in range(k):
            seq.append(str(i))
            if replay and replay[0] == i:
                seq.append("R")
        if replay:  # its own input, so nothing has to be held in memory while the pieces before it play
            ri = g.add_input(["-ss", f"{start + replay[1]:.3f}", "-t", f"{replay[2] - replay[1]:.3f}", "-i", str(src)])
            sp = replay[3]
            g.chains.append(f"[{ri}:v]setpts=(PTS-STARTPTS)/{sp},fps={FPS}[vR]")
            if info["audio"]:
                d = (replay[2] - replay[1]) / sp
                g.chains.append(f"[{ri}:a]asetpts=PTS-STARTPTS,atempo={sp},afade=t=in:d=0.05,"
                                f"afade=t=out:st={max(0.0, d - 0.08):.3f}:d=0.08[aR]")
        if info["audio"]:
            g.chains.append("".join(f"[v{x}][a{x}]" for x in seq) + f"concat=n={len(seq)}:v=1:a=1[vcut][acut]")
        else:
            g.chains.append("".join(f"[v{x}]" for x in seq) + f"concat=n={len(seq)}:v=1:a=0[vcut]")
    else:
        g.chains.append(f"[0:v]fps={FPS},setpts=PTS-STARTPTS[vcut]")

    n_split = sum(1 + len(br) for _, _, _, _, br in layers)
    split_labels = [g.label("s") for _ in range(n_split)]
    g.chains.append(f"[vcut]split={n_split}" + "".join(f"[{s}]" for s in split_labels))
    g.chains.append(f"color=c=black:s={OW}x{OH}:r={FPS}:d={L:.3f}[base0]")
    cur = "base0"
    si = iter(split_labels)
    for kind, crop, size, pos, branches in layers:
        s = next(si)
        lab = g.label("l")
        if kind == "blurbg":
            g.chains.append(f"[{s}]scale={OW}:{OH}:force_original_aspect_ratio=increase,crop={OW}:{OH},"
                            f"boxblur=30:3,eq=brightness=-0.16:saturation=1.2,setsar=1[{lab}]")
        else:
            x, y, w, h = crop
            g.chains.append(f"[{s}]crop={w}:{h}:{x}:{y},scale={size[0]}:{size[1]}:flags=lanczos,setsar=1[{lab}]")
        if kind == "bubble":
            mask_p, ring_p = circle_assets(size[0], work)
            mi = g.image(mask_p)
            ri = g.image(ring_p)
            ml, al = g.label("m"), g.label("a")
            g.chains.append(f"[{mi}:v]format=gray,scale={size[0]}:{size[1]}[{ml}]")
            g.chains.append(f"[{lab}][{ml}]alphamerge[{al}]")
            nxt = g.label("c")
            g.chains.append(f"[{cur}][{al}]overlay={pos[0]}:{pos[1]}[{nxt}]")
            cur = nxt
            nxt = g.label("c")
            g.chains.append(f"[{cur}][{ri}:v]overlay={pos[0] - 12}:{pos[1] - 12}[{nxt}]")
            cur = nxt
            continue
        nxt = g.label("c")
        g.chains.append(f"[{cur}][{lab}]overlay={pos[0]}:{pos[1]}[{nxt}]")
        cur = nxt
        for factor, enable, shk in branches:
            s2 = next(si)
            zl = g.label("z")
            zx, zy, zw, zh = tighter(crop, info, factor)
            g.chains.append(f"[{s2}]crop={zw}:{zh}:{zx}:{zy},scale={size[0]}:{size[1]}:flags=lanczos,setsar=1[{zl}]")
            nxt = g.label("c")
            g.chains.append(f"[{cur}][{zl}]overlay=x='{pos[0]}+{shk}':y={pos[1]}:enable='{enable}'[{nxt}]")
            cur = nxt

    if sep:
        nxt = g.label("c")
        g.chains.append(f"[{cur}]drawbox=x=0:y={sep - 5}:w={OW}:h=10:color=0x{ACCENT[0]:02X}{ACCENT[1]:02X}{ACCENT[2]:02X}:t=fill[{nxt}]")
        cur = nxt

    emoji = m.get("_emoji") or EMOJI.get(m["tag"], "🔥")
    if st["hook"] == "meme":
        hp, _ = hook_png(hook or " ", None, "meme", work / "hook.png")
        hi = g.image(hp)
        nxt = g.label("c")
        g.chains.append(f"[{cur}][{hi}:v]overlay=0:0[{nxt}]")
        cur = nxt

    cp, cw_, ch_ = credit_png(m, work / "credit.png")
    ci = g.image(cp)
    nxt = g.label("c")
    g.chains.append(f"[{cur}][{ci}:v]overlay={credit_xy[0]}:{credit_xy[1]}[{nxt}]")
    cur = nxt

    # reaction sticker pops at the peak (after the hook is gone)
    if zooms and p1 > 3.4 and cfg.get("reaction_sticker", True):
        e = emoji_png(emoji, 190)
        if e:
            e = e.rotate(10, resample=Image.BICUBIC, expand=True)
            sp = work / "sticker.png"
            e.save(sp)
            sti = g.image(sp)
            sl, nxt = g.label("e"), g.label("c")
            a = zooms[0][0]
            g.chains.append(f"[{sti}:v]format=rgba,fade=t=in:st={a:.3f}:d=0.1:alpha=1,"
                            f"fade=t=out:st={a + 1.15:.3f}:d=0.2:alpha=1[{sl}]")
            g.chains.append(f"[{cur}][{sl}]overlay=x={sticker_xy[0]}:y='{sticker_xy[1]}-30*max(0,1-(t-{a:.3f})/0.15)'"
                            f":enable='between(t,{a:.3f},{a + 1.4:.3f})'[{nxt}]")
            cur = nxt

    if st["flash"] and zooms:
        a = zooms[0][0]
        fl, nxt = g.label("f"), g.label("c")
        g.chains.append(f"color=c=white@0.5:s={OW}x{OH}:r={FPS}:d={L:.3f},format=rgba[{fl}]")
        g.chains.append(f"[{cur}][{fl}]overlay=0:0:enable='between(t,{a:.3f},{a + 0.1:.3f})'[{nxt}]")
        cur = nxt

    cap = build_captions(words, st["captions"], L, work, cfg.get("censor", True)) if words else None
    if cap:
        capi = g.add_input(["-f", "concat", "-safe", "0", "-i", str(cap)])
        cl, nxt = g.label("k"), g.label("c")
        g.chains.append(f"[{capi}:v]format=rgba,setpts=PTS-STARTPTS[{cl}]")
        g.chains.append(f"[{cur}][{cl}]overlay=0:{cap_y}:eof_action=pass[{nxt}]")
        cur = nxt

    if st["hook"] != "meme" and hook and hook_center is not None:
        hp, hh = hook_png(hook, emoji, st["hook"], work / "hook.png")
        hi = g.image(hp, 3.6)
        hl, nxt = g.label("h"), g.label("c")
        g.chains.append(f"[{hi}:v]format=rgba,fade=t=in:st=0:d=0.12:alpha=1,fade=t=out:st=3.1:d=0.4:alpha=1[{hl}]")
        hy = int(hook_center - 60 - (hh - 90) / 2)
        g.chains.append(f"[{cur}][{hl}]overlay=x=0:y='{hy}+40*max(0,1-t/0.2)':eof_action=pass[{nxt}]")
        cur = nxt

    # pop-up text from the edit plan (and a REPLAY label), placed where the hook sits once the hook is gone
    pops = []
    for tt, text, dur in m.get("_texts") or []:
        a = remap(tt - start)
        if hook and hook_center is not None:
            a = max(a, 3.75)
        if a < L - 0.3:
            pops.append((a, min(L, a + dur), text))
    if rspan:
        pops.append((rspan[0], rspan[1], "INSTANT REPLAY"))
    for i, (a, b, text) in enumerate(pops):
        tp, th = hook_png(text, None, "box" if st["hook"] == "meme" else st["hook"], work / f"pop{i}.png")
        ti = g.image(tp)
        tl, nxt = g.label("t"), g.label("c")
        ty = int((hook_center if hook_center is not None else OH * 0.42) - 60 - (th - 90) / 2)
        g.chains.append(f"[{ti}:v]format=rgba[{tl}]")
        g.chains.append(f"[{cur}][{tl}]overlay=x=0:y='{ty}+30*max(0,1-(t-{a:.3f})/0.15)'"
                        f":enable='between(t,{a:.3f},{b:.3f})'[{nxt}]")
        cur = nxt

    if st["bar"]:
        pb, nxt = g.label("p"), g.label("c")
        col = f"0x{ACCENT[0]:02X}{ACCENT[1]:02X}{ACCENT[2]:02X}"
        g.chains.append(f"color=c={col}:s={OW}x10:r={FPS}:d={L:.3f}[{pb}]")
        g.chains.append(f"[{cur}][{pb}]overlay=x='-w+w*t/{L:.3f}':y={OH - 10}[{nxt}]")
        cur = nxt

    g.chains.append(f"[{cur}]format=yuv420p[vout]")
    if info["audio"]:
        asrc = "[acut]" if cut else "[0:a]asetpts=PTS-STARTPTS,"
        g.chains.append(f"{asrc}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src)]
    for args in g.inputs:
        cmd += args
    graph_file = work / "graph.txt"
    graph_file.write_text(";\n".join(g.chains))
    cmd += ["-filter_complex_script", str(graph_file), "-map", "[vout]"]
    if info["audio"]:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-r", str(FPS), "-t", f"{L:.3f}", "-movflags", "+faststart"]
    br = ("12M", "16M", "24M") if m.get("_hq") else ("8M", "12M", "16M")  # best moments: higher bitrate
    enc_vt = ["-c:v", "h264_videotoolbox", "-b:v", br[0], "-maxrate", br[1], "-bufsize", br[2], "-profile:v", "high"]
    enc_sw = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18" if m.get("_hq") else "20", "-threads", "4"]
    try:
        _run(cmd + enc_vt + [str(out)], timeout=900)
    except RuntimeError:
        _run(cmd + enc_sw + [str(out)], timeout=1500)
    crop_n = None
    if cam_crop:
        x, y, w, h = cam_crop
        crop_n = [round(x / info["w"], 4), round(y / info["h"], 4), round(w / info["w"], 4), round(h / info["h"], 4)]
    return layout, L, crop_n


def edit_path(src, style_key):
    """9:16 edits sit in GTA6_CLIPS/<streamer>/<date>/, ready to post; the 16:9 recording stays in source/."""
    src = Path(src)
    folder = src.parent.parent if src.parent.name == "source" else src.parent
    return folder / f"{src.stem}_{style_key}.mp4"


WORDS = config.DATA / "words"


def full_words(src, mid, work, cfg):
    """Word timestamps for the whole recording, cached per clip (whisper runs once)."""
    src = Path(src)
    WORDS.mkdir(parents=True, exist_ok=True)
    f = WORDS / f"{mid}.json"
    st = src.stat()
    key = [st.st_size, round(st.st_mtime)]
    try:
        d = json.loads(f.read_text())
        if d.get("key") == key:
            return d["words"]
    except (OSError, ValueError):
        pass
    work.mkdir(parents=True, exist_ok=True)
    words = transcribe(src, 0.0, probe(src)["dur"], work, cfg)
    f.write_text(json.dumps({"key": key, "words": words}))
    return words


def _slice(words, start, end):
    return [{**w, "s": round(max(0.0, w["s"] - start), 3), "e": round(w["e"] - start, 3)}
            for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05]


# ---------------------------------------------------------------- edit plans (ideas + "tell me how to edit it")
_PLAIN = re.compile(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F\u200D]")


def _plain(text, n):
    """Text the caption fonts can draw: no emoji, single spaces, at most n characters (cut at a word)."""
    t = re.sub(r"\s+", " ", _PLAIN.sub("", str(text))).strip()
    if len(t) <= n:
        return t
    cut = t[:n - 1].rsplit(" ", 1)[0] if " " in t[:n - 1] else t[:n - 1]
    return cut.rstrip(",;:-") + "…"


def clean_plan(p, dur):
    """Keep only plan fields the editor understands, with sane values. Times are seconds in the recording."""
    if not isinstance(p, dict):
        return {}

    def num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    out = {}
    if isinstance(p.get("summary"), str) and p["summary"].strip():
        out["summary"] = _plain(p["summary"], 200)
    st, en = num(p.get("start")), num(p.get("end"))
    if st is not None or en is not None:
        st = min(max(0.0, st if st is not None else 0.0), max(0.0, dur - 4))
        en = min(dur, en if en is not None else dur)
        if en - st >= 4:
            out["start"], out["end"] = round(st, 2), round(min(en, st + 100), 2)
    rem = []
    for r in (p.get("remove") or [])[:12]:
        if isinstance(r, (list, tuple)) and len(r) == 2 and num(r[0]) is not None and num(r[1]) is not None \
                and r[1] > r[0]:
            rem.append([round(max(0.0, r[0]), 2), round(min(dur, r[1]), 2)])
    if rem:
        out["remove"] = rem
    for k in ("jump_cuts", "captions"):
        if isinstance(p.get(k), bool):
            out[k] = p[k]
    if p.get("style") in STYLES:
        out["style"] = p["style"]
    if p.get("length") in ("short", "long", "best"):
        out["length"] = p["length"]
    if isinstance(p.get("hook"), str):
        out["hook"] = censor(_plain(p["hook"], 60).upper())
    if p.get("caption_style") in CAPTION_STYLES:
        out["caption_style"] = p["caption_style"]
    if isinstance(p.get("emoji"), str) and 0 < len(p["emoji"].strip()) <= 4:
        out["emoji"] = p["emoji"].strip()
    zooms = sorted({round(z, 2) for z in (p.get("zooms") or [])[:8] if num(z) is not None and 0 <= z <= dur})
    if zooms:
        out["zooms"] = zooms
    texts = []
    for t in (p.get("texts") or [])[:8]:
        if isinstance(t, dict) and num(t.get("at")) is not None and isinstance(t.get("text"), str) \
                and _plain(t["text"], 40):
            texts.append({"at": round(min(max(0.0, t["at"]), dur), 2), "text": censor(_plain(t["text"], 40).upper()),
                          "dur": round(min(max(num(t.get("dur")) or 1.8, 0.6), 4.0), 2)})
    if texts:
        out["texts"] = texts
    r = p.get("replay")
    if isinstance(r, dict) and num(r.get("at")) is not None:
        out["replay"] = {"at": round(min(max(0.0, r["at"]), dur), 2),
                         "speed": min(max(num(r.get("speed")) or 0.5, 0.5), 1.0)}
    return out


def merge_plan(cur, new):
    """Apply an idea on top of the clip's plan. A new length means: pick the window again."""
    out = {**(cur or {}), **new}
    if "length" in new and "start" not in new:
        out.pop("start", None)
        out.pop("end", None)
    return out


def edit_ideas(m, style_key, kind, info, start, end, peak, words, has_cam, hook, plan, p_out):
    """Free suggestions from what the edit already knows (no AI). Each one is a small plan to apply."""
    ideas = []
    if has_cam and style_key != "reaction" and m["tag"] in ("funny", "hype", "spike", "manual"):
        ideas.append({"title": "Big reaction layout", "why": "His reaction is the payoff: give his face half the screen.",
                      "plan": {"style": "reaction"}})
    if not plan.get("replay"):
        ideas.append({"title": "Slow-motion instant replay",
                      "why": f"Replay the big moment ({peak:.0f} s) at half speed right after it.",
                      "plan": {"replay": {"at": round(peak, 2), "speed": 0.5}}})
    if p_out > 10 and "start" not in plan:
        ideas.append({"title": "Start closer to the action", "why": f"The payoff comes {p_out:.0f} s in. Start 7 s before it.",
                      "plan": {"start": round(max(0.0, peak - 7), 2), "end": round(end, 2)}})
    if p_out > 6 and not plan.get("texts"):
        ideas.append({"title": "\u201cWAIT FOR IT\u201d tease", "why": "Keeps people watching until the payoff.",
                      "plan": {"texts": [{"at": round(start + 3.8, 2), "text": "WAIT FOR IT", "dur": 2.2}]}})
    q = quote_near_peak(words, peak - start)
    if q and (hook or "").upper() != q.upper():
        q = q.strip('\u201c\u201d"')
        ideas.append({"title": f"Hook: \u201c{q}\u201d", "why": "Open with what he actually says at the peak.",
                      "plan": {"hook": q}})
    if kind == "short" and info["dur"] >= 45:
        ideas.append({"title": "Make it a 30–45 s highlight", "why": "There's more of the story in the recording.",
                      "plan": {"length": "long"}})
    return ideas[:4]


def make_edit(mid, style_key=None, length=None):
    cfg = config.load()
    m = store.get_moment(mid)
    if not m or not m.get("clip"):
        raise RuntimeError("clip not ready")
    plan = m.get("plan") or {}
    style_key = style_key or plan.get("style") or m.get("style") or cfg.get("edit_style", "streamer")
    if style_key not in STYLES:
        style_key = "streamer"
    src = Path(m["clip"])
    work = WORK / str(mid)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)  # scratch files from an earlier render of this clip
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    info = probe(src)
    env, hop = audio_envelope(src)
    start, end, peak = choose_window(m, info, env, hop, cfg)
    rec_kind = m.get("kind") or "short"
    kind = length if length in ("short", "long", "best") else (
        plan.get("length") or (m.get("edit_info") or {}).get("kind") or rec_kind)
    cut_cfg = cfg
    all_words = None
    if m["tag"] == "topclip":
        kind = "long" if end - start >= 28 else "short"
    elif kind in ("long", "best"):
        # one transcript of the whole recording; speech + loud parts decide where the highlight starts and ends
        all_words = full_words(src, mid, work, cfg)
        if kind == "best":
            lo, hi = cfg.get("best_min", 60), cfg.get("best_max", 90)
            cut_cfg = {**cfg, "cut_min_gap": 1.0}  # a minute-plus story: only cut real pauses
        else:
            lo, hi = cfg.get("long_min", 30), cfg.get("long_max", 45)
        win = long_window(all_words, env, hop, info["dur"], peak, {**cut_cfg, "long_min": lo, "long_max": hi},
                          force=kind == "best" or length == "long")
        if win:
            start, end = win
        else:
            kind = "short"  # too little going on around the peak for a long cut
    if "start" in plan and m["tag"] != "topclip":  # your edit plan picked the part of the recording to use
        start, end = max(0.0, plan["start"]), min(info["dur"], plan["end"])
    if all_words is None and ("start" in plan or kind != "short") and m["tag"] != "topclip":
        all_words = full_words(src, mid, work, cfg)
    words = _slice(all_words, start, end) if all_words is not None else transcribe(src, start, end - start, work, cfg)
    length = end - start
    segs = keep_segments(words, env, hop, start, length, peak, cut_cfg) if plan.get("jump_cuts", True) \
        else [(0.0, length)]
    if kind == "best" and "start" not in plan:
        # make sure the edit passes the minimum after jump cuts: widen the window (mostly lead-up) and cut again
        for _ in range(4):
            short = cfg.get("best_min", 60) - sum(b - a for a, b in segs)
            if short <= 0.5:
                break
            ns = max(0.0, start - short * 0.8)
            ne = min(info["dur"], end + max(0.0, short * 1.3 - (start - ns)))
            if ns >= start and ne <= end:
                break
            start, end = ns, ne
            length = end - start
            words = _slice(all_words, start, end)
            segs = keep_segments(words, env, hop, start, length, peak, cut_cfg)
    if plan.get("remove"):
        segs = subtract(segs, [(a - start, b - start) for a, b in plan["remove"]]) or segs
    m["_p2"] = second_peak(env, hop, start, end, peak)
    taken = [peak] + ([m["_p2"]] if m["_p2"] is not None else [])
    m["_peaks"] = extra_peaks(env, hop, start, end, taken, 5 if kind == "best" else 3) if kind in ("long", "best") else []
    m["_peaks"] += [z for z in plan.get("zooms", []) if start < z < end]
    m["_texts"] = [(t["at"], t["text"], t["dur"]) for t in plan.get("texts", []) if start <= t["at"] < end]
    rp = plan.get("replay")
    m["_replay"] = (rp["at"], rp["speed"]) if rp and start <= rp["at"] <= end else None
    m["_emoji"] = plan.get("emoji")
    m["_st"] = {"captions": plan["caption_style"]} if plan.get("caption_style") else None
    m["_hq"] = kind == "best"
    files = sample_frames(src, start, length, work)
    fc = store.facecam_get(store.facecam_key(m["platform"], m["stream_id"], m["channel"]))
    box, cam_src = None, "auto"
    if fc and fc.get("mode") == "none":
        face, cam, cam_src = None, None, "none"  # you said this streamer has no webcam: gameplay layouts
    elif fc and fc.get("mode") == "box" and fc.get("w"):
        box = {k: float(fc[k]) for k in ("x", "y", "w", "h")}
        face, cam, cam_src = detect_face_cluster(files, within=box), None, "drawn"
    else:
        face = detect_face_cluster(files)
        cam = _snap(face, files, info) if face else None
    if "hook" in plan:
        hook = plan["hook"] or None  # "" in a plan = no hook title
    else:
        hook = hook_text(m, words, cfg, peak - start)
    out = edit_path(src, style_key)
    layout, final_len, cam_crop = render(m, style_key, src, info, start, length, peak,
                                         [] if plan.get("captions") is False else words, face, cam, hook,
                                         work, cfg, out, segs, box=box)
    p_rel = peak - start
    p_out = sum(min(b, p_rel) - a for a, b in segs if a < p_rel)
    ideas = edit_ideas(m, style_key, kind, info, start, end, peak, words, bool(face or box), hook, plan, p_out)
    meta = {"hook_locked": bool((m.get("edit_info") or {}).get("hook_locked")) or bool(plan.get("hook")),
            "style": style_key, "kind": kind, "rec_kind": rec_kind,
            "layout": layout, "start": round(start, 2), "length": round(final_len, 2),
            "cuts": max(0, len(segs) - 1), "cut_s": round(max(0.0, length - final_len), 1),
            "peak": round(peak, 2), "words": len(words), "facecam": bool(face or box), "cam": cam_src,
            "box_face": bool(face) if box else None, "cam_crop": cam_crop,
            "cam_edges": [k for k, v in (cam or {}).items() if v is not None],
            "replay": bool(m["_replay"]), "pops": len(m["_texts"]), "summary": plan.get("summary"),
            "ideas": ideas, "render_s": round(time.time() - t0, 1),
            "transcript": " ".join(w["w"] for w in words)[:600]}
    store.update_moment(mid, vertical=str(out), hook=hook or "", style=style_key, edit_info=json.dumps(meta))
    return out
