"""Moment scoring: chat speed vs the stream's own normal, laughs, hype, "clip it" calls, loudness."""
import re
import statistics
import time
from collections import deque

WINDOW = 8          # seconds of chat looked at per tick
BASELINE = 180      # seconds of history that define "normal"
KEEP_MSGS = 420     # chat kept for clip samples (the quota can clip a moment from minutes ago)


def _tokenizer(tokens):
    words = sorted({t.lower() for t in tokens if t[0].isalnum()}, key=len, reverse=True)
    pat = re.compile(r"(?<![\w])(" + "|".join(re.escape(w) for w in words) + r")(?![\w])", re.I) if words else None
    symbols = [t for t in tokens if not t[0].isalnum()]

    def hits(text):
        n = len(pat.findall(text)) if pat else 0
        return n + sum(text.count(s) for s in symbols)

    return hits


class StreamScore:
    def __init__(self, cfg):
        self.msgs = deque()               # (t, user, text, laugh, hype, clip)
        self.rates = deque()              # (t, msgs/s)
        self.vols = deque()               # (t, mean dB)
        self.history = deque(maxlen=90)   # recent scores for the sparkline
        self.minutes = deque(maxlen=90)   # [minute, msgs, laughs, hype, clip calls] for the scout
        self.spikes = deque(maxlen=500)   # times the score crossed the threshold
        self.started = time.time()
        self.last_fire = 0
        self.last_spike = 0
        self.has_chat = False
        self.total_msgs = 0
        self.configure(cfg)

    def configure(self, cfg):
        self.cfg = cfg
        self.laugh = _tokenizer(cfg["laugh_tokens"])
        self.hype = _tokenizer(cfg["hype_tokens"])
        self.clip = _tokenizer(cfg["clip_tokens"])

    def add_msg(self, user, text, t=None):
        t = t or time.time()
        self.has_chat = True
        self.total_msgs += 1
        low = text.lower()
        lg, hy, cl = self.laugh(low) > 0, self.hype(low) > 0, self.clip(low) > 0
        self.msgs.append((t, user, text, lg, hy, cl))
        mk = int(t // 60)
        if not self.minutes or self.minutes[-1][0] != mk:
            self.minutes.append([mk, 0, 0, 0, 0])
        b = self.minutes[-1]
        b[1] += 1
        b[2] += lg
        b[3] += hy
        b[4] += cl

    def add_volume(self, t, db):
        self.vols.append((t, db))

    @staticmethod
    def _z(value, series, floor, rel=0.0):
        """How many 'normal wobbles' above this stream's own average the value is."""
        if len(series) < 6:
            return 0.0
        mean = statistics.fmean(series)
        sd = max(statistics.pstdev(series), floor, abs(mean) * rel)
        return (value - mean) / sd

    def tick(self, now=None):
        """Return (score, parts, tag). Call every ~2 s."""
        now = now or time.time()
        while self.msgs and self.msgs[0][0] < now - KEEP_MSGS:
            self.msgs.popleft()
        while self.rates and self.rates[0][0] < now - BASELINE:
            self.rates.popleft()
        while self.vols and self.vols[0][0] < now - BASELINE:
            self.vols.popleft()

        win = [m for m in self.msgs if m[0] >= now - WINDOW]
        rate = len(win) / WINDOW
        base_rates = [r for t, r in self.rates if t < now - WINDOW]
        chat_z = self._z(rate, base_rates, 0.3, rel=0.15) if self.has_chat else 0.0
        self.rates.append((now, rate))

        n = len(win)
        laugh = sum(m[3] for m in win) / n if n >= 4 else 0.0
        hype = sum(m[4] for m in win) / n if n >= 4 else 0.0
        clips = sum(m[5] for m in win)

        audio_z = 0.0
        if self.vols:
            vt, vdb = self.vols[-1]
            if now - vt < 20:
                audio_z = self._z(vdb, [v for t, v in self.vols if t < vt], 1.5)

        if self.has_chat:
            score = max(chat_z, 0) + 4 * laugh + 2.5 * hype + 1.5 * min(clips, 3) + 0.7 * max(audio_z, 0)
        else:  # audio only (Kick)
            score = 1.6 * max(audio_z, 0)

        parts = {
            "chat_z": round(chat_z, 2), "rate": round(rate, 2), "laugh": round(laugh, 2),
            "hype": round(hype, 2), "clip_calls": clips, "audio_z": round(audio_z, 2),
        }
        if laugh >= max(hype, 0.15):
            tag = "funny"
        elif hype >= 0.15:
            tag = "hype"
        elif audio_z > 2 and audio_z * 0.7 >= max(chat_z, 0):
            tag = "loud"
        else:
            tag = "spike"
        self.history.append(round(score, 2))
        c = self.cfg
        if (score >= c["threshold"] and now - self.started >= c["warmup"]
                and now - self.last_spike >= c["cooldown"]):
            self.spikes.append(now)
            self.last_spike = now
        return score, parts, tag

    def stats(self, window_min=10, now=None):
        """Chat activity over the last few minutes, used to rank streamers."""
        now = now or time.time()
        observed = (now - self.started) / 60
        cut = int(now // 60) - window_min
        rows = [b for b in self.minutes if b[0] > cut]
        msgs = sum(b[1] for b in rows)
        span = max(1.0, min(window_min, observed))
        hours = max(0.25, min(60.0, observed) / 60)
        recent_spikes = sum(1 for t in self.spikes if t > now - 3600)
        return {
            "observed_min": round(observed, 1),
            "chat_per_min": round(msgs / span, 1),
            "laugh_rate": round(sum(b[2] for b in rows) / msgs, 3) if msgs else 0.0,
            "hype_rate": round(sum(b[3] for b in rows) / msgs, 3) if msgs else 0.0,
            "clip_calls": sum(b[4] for b in rows),
            "spikes_per_hour": round(recent_spikes / hours, 2),
            "spikes": recent_spikes,
        }

    def should_fire(self, score, now=None, threshold=None, cooldown=None):
        now = now or time.time()
        c = self.cfg
        if now - self.started < c["warmup"] or now - self.last_fire < (cooldown or c["cooldown"]):
            return False
        if score >= (threshold if threshold is not None else c["threshold"]):
            self.last_fire = now
            return True
        return False

    def sample_chat(self, now=None, before=20, after=5, limit=25):
        now = now or time.time()
        rows = [m for m in self.msgs if now - before <= m[0] <= now + after]
        rows.sort(key=lambda m: (m[3] or m[4] or m[5]), reverse=True)
        return [{"user": m[1], "text": m[2][:200]} for m in rows[:limit]]

    @staticmethod
    def reasons(parts, tag):
        r = []
        if parts["chat_z"] >= 2:
            r.append(f"chat {parts['chat_z']:.1f}× above normal ({parts['rate']:.1f} msg/s)")
        if parts["laugh"] >= 0.15:
            r.append(f"{round(parts['laugh'] * 100)}% laughing")
        if parts["hype"] >= 0.15:
            r.append(f"{round(parts['hype'] * 100)}% shock/hype")
        if parts["clip_calls"]:
            r.append(f"{parts['clip_calls']}× \"clip it\"")
        if parts["audio_z"] >= 2:
            r.append(f"audio spike ({parts['audio_z']:.1f}σ)")
        return r or [tag]
