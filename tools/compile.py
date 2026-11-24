"""Funny compilations from ClipWatch's short 9:16 edits.

    .venv/bin/python tools/compile.py [--count 5] [--per 8] [--out ~/Desktop/GTA6/Funny Compilations]

Picks short clips (kept first, then "funny", then the rest by score), mixes streamers so the same one never
plays twice in a row, and joins them with an animated title card and a different transition between clips.
Clips already used in a compilation are skipped next time (data/compiled.json). Never deletes anything.
"""
import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cw import editor  # noqa: E402  (fonts)

import shutil  # noqa: E402
FF, FP = shutil.which("ffmpeg") or "ffmpeg", shutil.which("ffprobe") or "ffprobe"
W, H, FPS, T = 1080, 1920, 30, 0.45
USED = ROOT / "data" / "compiled.json"
TRANSITIONS = ["slideleft", "circleopen", "zoomin", "slideup", "smoothleft", "radial", "pixelize", "wipeleft",
               "circleclose", "smoothup"]
RANK = {"funny": 0, "hype": 1, "spike": 2, "loud": 3, "manual": 3, "topclip": 4}


def probe(p):
    d = json.loads(subprocess.run([FP, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                                   "-of", "json", str(p)], capture_output=True, text=True).stdout)
    return float(d["format"]["duration"]), any(s["codec_type"] == "audio" for s in d["streams"])


def pick(count, per, max_len=25.0):
    db = sqlite3.connect(ROOT / "data" / "clipwatch.db")
    db.row_factory = sqlite3.Row
    used = set(json.loads(USED.read_text())) if USED.exists() else set()
    rows = db.execute("SELECT id, channel, tag, score, status, hook, vertical FROM moments WHERE vertical IS NOT NULL "
                      "AND edit_state='done' AND cleaned IS NULL AND status IN ('ready','kept')").fetchall()
    pool = []
    for r in rows:
        v = Path(r["vertical"])
        if r["id"] in used or not v.exists():
            continue
        dur, audio = probe(v)
        if 6 <= dur <= max_len:
            pool.append({**dict(r), "dur": dur, "audio": audio})
    pool.sort(key=lambda c: (c["status"] != "kept", RANK.get(c["tag"], 3), -(c["score"] or 0)))
    pool = pool[:count * per]
    comps = [[] for _ in range(count)]
    # deal the best clips across compilations, never the same streamer twice in a row
    for c in pool:
        for comp in sorted(comps, key=len):
            if len(comp) < per and (not comp or comp[-1]["channel"] != c["channel"]):
                comp.append(c)
                break
    return [c for c in comps if len(c) >= 4]


def title_card(path, part):
    im = Image.new("RGB", (W, H), (13, 14, 18))
    d = ImageDraw.Draw(im)
    accent, yellow = (255, 90, 95), (255, 225, 77)
    d.rectangle([0, 0, W, 14], fill=accent)
    d.rectangle([0, H - 14, W, H], fill=accent)
    lines = [("GTA 6", 170, yellow), ("STREAMERS", 120, (255, 255, 255)), ("FUNNIEST", 150, (255, 255, 255)),
             ("MOMENTS", 150, (255, 255, 255)), (f"PART {part}", 90, accent)]
    y = 560
    for text, size, col in lines:
        f = editor.font("black", size)
        w = d.textlength(text, font=f)
        d.text(((W - w) / 2, y), text, font=f, fill=col, stroke_width=8 if size > 100 else 0, stroke_fill=(0, 0, 0))
        y += int(size * 1.18)
    im.save(path)


def render(clips, part, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = ROOT / "data" / "compile"
    tmp.mkdir(parents=True, exist_ok=True)
    card = tmp / f"title_{part}.png"
    title_card(card, part)
    intro = 1.6
    args, chains = [FF, "-y", "-hide_banner", "-loglevel", "error"], []
    args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{intro}", "-i", str(card)]
    for c in clips:
        args += ["-i", c["vertical"]]
    n = len(clips)
    # title: slow zoom-in punch, silent
    chains.append(f"[0:v]scale={W * 2}:{H * 2},zoompan=z='min(1+0.004*on,1.12)':x='iw/2-(iw/zoom/2)':"
                  f"y='ih/2-(ih/zoom/2)':d={int(intro * FPS)}:s={W}x{H}:fps={FPS},setsar=1,format=yuv420p[v0]")
    chains.append(f"aevalsrc=0:c=stereo:s=48000:d={intro}[a0]")
    durs = [intro]
    for i, c in enumerate(clips, 1):
        chains.append(f"[{i}:v]scale={W}:{H},setsar=1,fps={FPS},format=yuv420p,settb=AVTB[v{i}]")
        if c["audio"]:
            chains.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]")
        else:
            chains.append(f"aevalsrc=0:c=stereo:s=48000:d={c['dur']:.3f}[a{i}]")
        durs.append(c["dur"])
    chains[0] = chains[0].replace("format=yuv420p[v0]", "format=yuv420p,settb=AVTB[v0]")
    vprev, aprev, acc = "v0", "a0", durs[0]
    for i in range(1, n + 1):
        tr = TRANSITIONS[(part * 3 + i) % len(TRANSITIONS)]
        off = acc - T
        chains.append(f"[{vprev}][v{i}]xfade=transition={tr}:duration={T}:offset={off:.3f}[x{i}]")
        chains.append(f"[{aprev}][a{i}]acrossfade=d={T}:c1=tri:c2=tri[y{i}]")
        vprev, aprev = f"x{i}", f"y{i}"
        acc = off + durs[i]
    graph = tmp / f"graph_{part}.txt"
    graph.write_text(";\n".join(chains))
    out = out_dir / f"GTA 6 Funniest Streamer Moments - Part {part}.mp4"
    tail = ["-filter_complex_script", str(graph), "-map", f"[{vprev}]", "-map", f"[{aprev}]",
            "-c:a", "aac", "-b:a", "192k", "-r", str(FPS), "-movflags", "+faststart"]
    vt = ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-maxrate", "12M", "-bufsize", "16M", "-profile:v", "high"]
    sw = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "4"]
    r = subprocess.run(args + tail + vt + [str(out)], capture_output=True, text=True)
    if r.returncode:
        r = subprocess.run(args + tail + sw + [str(out)], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr[-800:])
    credits = out.with_suffix(".txt")
    credits.write_text(f"GTA 6 Funniest Streamer Moments - Part {part}\n\nIn this video:\n" + "\n".join(
        f"{i}. {c['channel']} - {c['hook'] or c['tag']}" for i, c in enumerate(clips, 1)) + "\n\n#gta6 #gta6funny #streamers\n")
    return out, acc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--per", type=int, default=8)
    ap.add_argument("--start", type=int, default=1, help="number of the first part")
    ap.add_argument("--out", default=str(Path.home() / "Desktop" / "GTA6" / "Funny Compilations"))
    a = ap.parse_args()
    comps = pick(a.count, a.per)
    used = set(json.loads(USED.read_text())) if USED.exists() else set()
    for k, clips in enumerate(comps):
        part = a.start + k
        out, length = render(clips, part, Path(a.out).expanduser())
        used |= {c["id"] for c in clips}
        USED.write_text(json.dumps(sorted(used)))
        print(f"Part {part}: {len(clips)} clips, {length:.0f} s -> {out}", flush=True)


if __name__ == "__main__":
    main()
