"""Scout: listens to chat on many live GTA 6 streams at once and ranks who to record.

Clip Potential Score (0–100) = weighted percentile ranks among the current candidates:
  laughs      share of chat messages that are laughing (KEKW, LUL, 😂 …)
  spikes      how often chat explodes (score over the threshold) per hour
  engagement  chat messages per minute per 1,000 viewers (how much chat interacts with them)
  viewers     audience size
  clips       clips viewers made of them in the last 24 h + their views (Twitch, needs keys)
then × your own history: streamers whose clips you keep rank up, rejected ones rank down.
Autopilot records the top `max_streams`, and swaps one only when another scores clearly higher.
"""
import threading
import time

from . import config, discover, store


def _pct_ranks(values):
    """Percentile rank (0..1) for each non-None value; None stays None."""
    known = sorted(v for v in values if v is not None)
    if not known:
        return [None] * len(values)
    out = []
    for v in values:
        if v is None:
            out.append(None)
        elif len(known) == 1:
            out.append(0.5)
        else:
            below = sum(1 for k in known if k < v)
            equal = sum(1 for k in known if k == v)
            out.append((below + (equal - 1) / 2) / (len(known) - 1))
    return out


class Scout:
    def __init__(self, engine):
        self.e = engine
        self.cands = {}        # sid -> candidate dict (meta)
        self.ranked = []
        self.notes = []
        self.status = "starting"
        self.last_refresh = 0
        self.first_refresh = 0
        self.last_swap = 0
        self.ignored = set()
        self.lock = threading.RLock()
        threading.Thread(target=self._loop, daemon=True, name="scout").start()

    # ---- candidate pool ----
    def refresh(self):
        cfg = config.load()
        self.status = "searching"
        found = discover.discover_all()
        self.e.discovered = found
        favs = discover.check_favorites(cfg.get("favorites", []))
        pool = {}
        for c in favs + found["streams"]:
            try:
                _, _, sid = discover.parse_url(c["url"])
            except ValueError:
                continue
            c = {**pool.get(sid, {}), **c}
            c["id"] = sid
            c["gta"] = discover.is_gta(c.get("title"), c.get("category"))
            pool[sid] = c
        # Twitch clip stats (only with keys)
        if config.has_keys("twitch"):
            for c in sorted(pool.values(), key=lambda c: -(c.get("viewers") or 0))[:12]:
                if c["platform"] == "twitch" and c.get("user_id"):
                    try:
                        c.update(discover.twitch_clip_stats(c["user_id"]))
                    except Exception:  # noqa: BLE001
                        pass
        eligible = [c for c in pool.values() if c["gta"] or not cfg.get("gta_only", True)]
        eligible.sort(key=lambda c: (not c.get("fav"), -(c.get("viewers") or 0)))
        keep, yt = [], 0
        for c in eligible:
            if len(keep) >= cfg.get("scout_max", 16):
                break
            if c["platform"] == "youtube":
                if yt >= cfg.get("scout_youtube_max", 6):
                    continue
                yt += 1
            keep.append(c)
        keep_ids = {c["id"] for c in keep}
        with self.lock:
            for sid in list(self.cands):
                if sid not in keep_ids:
                    self.e.unlisten(sid, "scout")
                    del self.cands[sid]
            for c in keep:
                self.cands[c["id"]] = {**self.cands.get(c["id"], {}), **c}
                self.e.listen(c, "scout")
            # live-but-not-GTA favorites are shown, not scouted
            self.offtopic = [c for c in pool.values() if c.get("fav") and not c["gta"]]
        self.notes = found["notes"]
        self.last_refresh = time.time()
        self.first_refresh = self.first_refresh or self.last_refresh
        self.status = "scouting"

    # ---- ranking ----
    def rank(self):
        cfg = config.load()
        w = cfg.get("scout_weights", {})
        hist = store.channel_stats()
        with self.lock:
            cands = list(self.cands.values())
            # watched streams the scout didn't find are ranked too
            for sid, st in self.e.streams.items():
                if sid not in self.cands:
                    cands.append({**st["meta"], "id": sid, "gta": True, "viewers": 0})
        rows = []
        for c in cands:
            lis = self.e.listeners.get(c["id"])
            stats = lis["score"].stats() if lis else {}
            has_chat = c["platform"] != "kick"
            viewers = c.get("viewers") or None
            cpm = stats.get("chat_per_min")
            rows.append({
                **{k: c.get(k) for k in ("id", "platform", "channel", "display", "title", "url", "thumb", "country", "fav")},
                "viewers": viewers or 0,
                "stats": stats,
                "laughs": stats.get("laugh_rate") if has_chat and cpm else None,
                "spikes": stats.get("spikes_per_hour") if has_chat and stats.get("observed_min", 0) >= 3 else None,
                "engagement": round(cpm / max(viewers / 1000, 0.3), 2) if (has_chat and viewers and cpm) else None,
                "viewers_m": viewers,
                "clips_m": (c.get("clips", 0) + c.get("clip_views", 0) / 1000) if "clips" in c else None,
                "clips": c.get("clips"), "clip_views": c.get("clip_views"),
                "hist": hist.get(store.history_key(c["platform"], c["id"], c.get("display") or c.get("channel")),
                                 {"n": 0, "kept": 0, "rejected": 0}),
                "warming": stats.get("observed_min", 0) < 3,
                "key": store.facecam_key(c["platform"], c["id"], c.get("display") or c.get("channel")),
                "watching": c["id"] in self.e.streams,
                "auto": bool(self.e.streams.get(c["id"], {}).get("meta", {}).get("auto")),
            })
        keys = {"laughs": "laughs", "spikes": "spikes", "engagement": "engagement", "viewers": "viewers_m", "clips": "clips_m"}
        ranks = {k: _pct_ranks([r[col] for r in rows]) for k, col in keys.items()}
        for i, r in enumerate(rows):
            num = den = 0.0
            parts = {}
            for k in keys:
                wt = float(w.get(k, 0))
                if wt <= 0:
                    continue
                v = ranks[k][i]
                parts[k] = None if v is None else round(v, 2)
                num += wt * (0.35 if v is None else v)  # unknown counts a bit below average
                den += wt
            cps = num / den if den else 0
            h = r["hist"]
            reviewed = h["kept"] + h["rejected"]
            if reviewed >= 3:
                cps *= 0.75 + 0.5 * h["kept"] / reviewed
            if r.get("fav"):
                cps += 0.10  # your US/UK list
            r["parts"] = parts
            r["cps"] = round(max(0.0, min(1.0, cps)) * 100)
        rows.sort(key=lambda r: (r["warming"], -r["cps"]))
        self.ranked = rows
        return rows

    # ---- autopilot ----
    def autopilot(self):
        cfg = config.load()
        if not cfg.get("autopilot"):
            return
        if not self.first_refresh or time.time() - self.first_refresh < 180:
            return  # listen to chat for 3 minutes before picking anyone
        rows = self.rank()
        n = cfg["max_streams"]
        # drop streams that ended (5 min offline) so their slot is free; the scout re-finds them if they return
        for sid, st in list(self.e.streams.items()):
            if st["status"] == "offline" and time.time() - st.get("offline_at", time.time()) > 300:
                self.e.unwatch(sid)
                self.notes = [f"{st['meta'].get('channel') or sid} went offline, slot freed"]
        active = [sid for sid, st in self.e.streams.items() if st["status"] != "offline"]
        rules = store.rules_all()
        pool = [r for r in rows if not r["watching"] and not r["warming"]
                and store.effective_rule(cfg, r["key"], rules)["record"]]  # Record off on the Rules board = never
        # fill free slots
        while len(active) < n and pool:
            best = pool.pop(0)
            try:
                self.e.watch(best["url"], best.get("title", ""), best.get("display") or best["channel"], auto=True)
                active.append(best["id"])
            except ValueError:
                break
        # swap the weakest autopilot pick when someone is clearly better (at most every 5 min)
        if not pool or time.time() - self.last_swap < 300:
            return
        min_watch = cfg.get("autopilot_min_watch_min", 15) * 60
        mine = [r for r in rows if r["watching"] and r["auto"]
                and time.time() - self.e.streams[r["id"]]["since"] > min_watch]
        if not mine:
            return
        weakest = min(mine, key=lambda r: r["cps"])
        best = pool[0]
        if best["cps"] >= weakest["cps"] + cfg.get("autopilot_margin", 12):
            self.e.unwatch(weakest["id"])
            try:
                self.e.watch(best["url"], best.get("title", ""), best.get("display") or best["channel"], auto=True)
                self.notes = [f"Autopilot: swapped {weakest['display'] or weakest['channel']} → {best['display'] or best['channel']}"]
            except ValueError:
                pass
            self.last_swap = time.time()

    def _pause(self):
        """ClipWatch is switched off: stop reading every scouted chat; scan again when it's back on."""
        with self.lock:
            for sid in list(self.cands):
                self.e.unlisten(sid, "scout")
            self.cands = {}
        self.ranked, self.status, self.last_refresh, self.first_refresh = [], "off", 0, 0

    def _loop(self):
        time.sleep(3)
        while True:
            cfg = config.load()
            if not cfg.get("running", True):
                if self.status != "off":
                    self._pause()
                time.sleep(3)
                continue
            try:
                if cfg.get("scout", True) and time.time() - self.last_refresh > cfg.get("scout_refresh_min", 5) * 60:
                    self.refresh()
                self.rank()
                self.autopilot()
            except Exception as e:  # noqa: BLE001 — keep scouting
                self.notes = [f"Scout error: {e}"]
                self.status = "error"
                self.last_refresh = time.time()
            time.sleep(15)

    def snapshot(self):
        return {"status": self.status, "notes": self.notes, "ranked": self.ranked, "last_refresh": self.last_refresh,
                "offtopic": getattr(self, "offtopic", []), "ignored": self._off_ids()}

    def _off_ids(self):
        cfg, rules = config.load(), store.rules_all()
        return sorted(r["id"] for r in self.ranked if not store.effective_rule(cfg, r["key"], rules)["record"])
