"""Rolling buffer: yt-dlp pulls the live stream, ffmpeg writes 5-second .ts chunks.

Chunks are named seg_<unix time>.ts. Only the last `buffer_minutes` are kept — this folder
is a cache by design, it is the only place ClipWatch removes files.
"""
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config

VOL_RE = re.compile(r"mean_volume:\s*(-?[\d.]+) dB")


def measure_volume(path):
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=20,
        )
        m = VOL_RE.search(out.stderr)
        return float(m.group(1)) if m else None
    except (subprocess.TimeoutExpired, ValueError):
        return None


def seg_time(p):
    try:
        return int(p.stem.split("_")[1])
    except (IndexError, ValueError):
        return 0


class Recorder:
    def __init__(self, stream, on_audio, on_status):
        self.s = stream
        self.dir = config.BUFFER / stream["id"].replace(":", "_").replace("@", "")
        self.on_audio = on_audio
        self.on_status = on_status
        self.procs = []
        self.stop_ev = threading.Event()
        self.measured = set()
        self.fails = 0
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"rec-{stream['id']}")

    # ---- lifecycle ----
    def start(self):
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)  # stale cache from a previous run
        self.dir.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def stop(self):
        self.stop_ev.set()
        self._kill()

    def _kill(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs = []

    def _spawn(self):
        cfg = config.load()
        out_args = ["-c", "copy", "-f", "segment", "-segment_time", str(cfg["segment_seconds"]),
                    "-segment_format", "mpegts", "-reset_timestamps", "1", "-strftime", "1",
                    str(self.dir / "seg_%s.ts")]
        if self.s["platform"] == "youtube":
            # YouTube live ships video and audio as separate HLS playlists: let ffmpeg read both.
            fmt = cfg["quality"].replace("best[", "bv*[").split("/")[0] + "+ba/" + cfg["quality"]
            # only a stream that is live right now — an ended stream would silently become its replay
            res = subprocess.run(["yt-dlp", "-q", "--no-warnings", "--match-filter", "live_status=is_live",
                                  "-g", "-f", fmt, self.s["url"]],
                                 capture_output=True, text=True, timeout=90)
            urls = [u for u in res.stdout.split() if u.startswith("http")]
            if not urls:
                raise RuntimeError(res.stderr.strip()[-200:] or "stream is not live")
            ins = []
            for u in urls:
                ins += ["-i", u]
            maps = ["-map", "0:v:0?", "-map", f"{len(urls) - 1}:a:0?"]
            ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", *ins, *maps, *out_args],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.procs = [ff]
        else:
            # Twitch / Kick: streamlink (skips Twitch ad breaks instead of recording "Commercial break").
            sl = str(config.ROOT / ".venv" / "bin" / "streamlink")
            args = [sl, "--stdout", "--loglevel", "error", "--stream-segment-threads", "2"]
            if self.s["platform"] == "twitch":
                args += ["--twitch-disable-ads", "--twitch-low-latency"]
            src = subprocess.Popen(args + [self.s["url"], cfg.get("stream_quality", "1080p60,1080p,720p60,720p,best")],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ff = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                 "-map", "0:v:0?", "-map", "0:a:0?", *out_args],
                stdin=src.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            src.stdout.close()
            self.procs = [src, ff]
        self.started = time.time()

    def _safe_spawn(self):
        try:
            self._spawn()
            return True
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            self.procs = []
            self.on_status(self.s["id"], "reconnecting", str(e))
            return False

    # ---- buffer access ----
    def segments(self):
        return sorted(self.dir.glob("seg_*.ts"), key=seg_time)

    def segments_between(self, t0, t1):
        seg = config.load()["segment_seconds"]
        return [p for p in self.segments() if t0 - seg * 2 <= seg_time(p) <= t1]

    def buffered_seconds(self):
        segs = self.segments()
        return (seg_time(segs[-1]) - seg_time(segs[0])) if len(segs) > 1 else 0

    # ---- main loop ----
    def _run(self):
        self.on_status(self.s["id"], "connecting")
        self._safe_spawn()
        last_new = time.time()
        while not self.stop_ev.wait(2):
            cfg = config.load()
            segs = self.segments()
            # completed chunks = all except the one being written
            for p in segs[:-1]:
                if p.name not in self.measured:
                    self.measured.add(p.name)
                    last_new = time.time()
                    v = measure_volume(p)
                    if v is not None:
                        self.on_audio(self.s["id"], seg_time(p), v)
            if segs[:-1]:
                self.on_status(self.s["id"], "live")
            # ring buffer
            cutoff = time.time() - cfg["buffer_minutes"] * 60
            for p in segs:
                if seg_time(p) < cutoff:
                    p.unlink(missing_ok=True)
                    self.measured.discard(p.name)
            # health
            dead = not self.procs or any(p.poll() is not None for p in self.procs)
            # Twitch pauses output during ad breaks (we skip them), so allow a long gap there
            stalled = time.time() - last_new > (240 if self.s["platform"] == "twitch" else 75)
            if dead or stalled:
                err = ""
                for p in self.procs:
                    if p.poll() is not None and p.stderr:
                        try:
                            err = (p.stderr.read() or b"").decode("utf-8", "ignore").strip()[-200:] or err
                        except (OSError, ValueError):
                            pass
                self._kill()
                self.fails += 1
                if self.fails >= 4:
                    self.on_status(self.s["id"], "offline", err or "stream ended or unreachable")
                    return
                self.on_status(self.s["id"], "reconnecting", err)
                if self.stop_ev.wait(5 * self.fails):
                    return
                self._safe_spawn()
                last_new = time.time()
            elif segs[:-1]:
                self.fails = 0
        self._kill()
