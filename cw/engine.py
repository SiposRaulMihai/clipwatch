"""Ties it together: chat listeners (shared by watched + scouted streams), recorders, scoring, clip jobs."""
import subprocess
import threading
import time
from collections import deque

from . import clipper, config, discover, store
from .chat import TwitchChat, YouTubeChat
from .detector import StreamScore
from .recorder import Recorder

TICK = 2


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.streams = {}      # sid -> watched (recording) stream
        self.listeners = {}    # sid -> {"score", "chat", "refs", "meta"} — chat for watched AND scouted streams
        self.discovered = {"streams": [], "notes": [], "at": 0}
        self.top_clips = {"clips": [], "note": "", "at": 0}
        self.twitch = TwitchChat(self._on_msg)
        self.running = None  # the master switch; the first loop tick starts (or keeps off) the watch list
        threading.Thread(target=self._loop, daemon=True, name="engine").start()
        from .scout import Scout
        self.scout = Scout(self)

    # ---- chat listeners ----
    def listen(self, meta, who):
        sid = meta["id"]
        with self.lock:
            lis = self.listeners.get(sid)
            if not lis:
                lis = {"score": StreamScore(config.load()), "chat": None, "refs": set(), "meta": meta}
                self.listeners[sid] = lis
                if meta["platform"] == "twitch":
                    self.twitch.join(sid.split(":", 1)[1])
                elif meta["platform"] == "youtube":
                    lis["chat"] = YouTubeChat(sid, meta["url"], self._on_msg)
            lis["refs"].add(who)
            return lis

    def unlisten(self, sid, who):
        with self.lock:
            lis = self.listeners.get(sid)
            if not lis:
                return
            lis["refs"].discard(who)
            if lis["refs"]:
                return
            del self.listeners[sid]
        if lis["chat"]:
            lis["chat"].stop()
        if sid.startswith("twitch:"):
            self.twitch.part(sid.split(":", 1)[1])

    def _on_msg(self, sid, user, text):
        lis = self.listeners.get(sid)
        if lis:
            lis["score"].add_msg(user, text)

    def _on_audio(self, sid, t, db):
        st = self.streams.get(sid)
        if st:
            st["score"].add_volume(t, db)

    def _on_status(self, sid, status, err=""):
        st = self.streams.get(sid)
        if st:
            if status == "offline" and st["status"] != "offline":
                st["offline_at"] = time.time()
            st["status"] = status
            if err:
                st["error"] = err

    # ---- watch / unwatch ----
    def watch(self, url, title="", display="", auto=False):
        platform, channel, sid = discover.parse_url(url)
        with self.lock:
            if sid in self.streams:
                if self.streams[sid]["status"] != "offline":
                    if not auto:
                        self.streams[sid]["meta"]["auto"] = False  # you picked it: autopilot leaves it alone
                    return sid
                self.unwatch(sid)  # it went offline earlier and is live again: start fresh
            live = [s for s in self.streams.values() if s["status"] != "offline"]
            if len(live) >= config.load()["max_streams"]:
                raise ValueError(f"Already recording {len(live)} streams (max_streams). Stop one first.")
            s = {"id": sid, "url": discover.canonical_url(platform, channel), "platform": platform,
                 "channel": display or channel, "title": title, "auto": auto}
            store.watch_add(s)
            if self.running:
                self._start(s)  # while ClipWatch is off it only goes on the list
        return sid

    def _start(self, s):
        s.setdefault("auto", False)
        lis = self.listen(s, "watch")
        st = {"meta": s, "status": "starting", "error": "", "since": time.time(),
              "score": lis["score"], "last": {}, "moments": 0}
        self.streams[s["id"]] = st
        st["rec"] = Recorder(s, self._on_audio, self._on_status)
        st["rec"].start()
        if not s.get("title"):
            threading.Thread(target=self._fetch_title, args=(s,), daemon=True).start()

    def set_running(self, on):
        """Master switch. Off: every recorder and chat reader stops (the watch list is kept). On: they start again."""
        with self.lock:
            if on == self.running:
                return
            self.running = on
            if on:
                for s in store.watch_list():
                    if s["id"] not in self.streams:
                        try:
                            self._start(s)
                        except Exception:  # noqa: BLE001
                            pass
            else:
                for sid, st in list(self.streams.items()):
                    st["rec"].stop()
                    self.unlisten(sid, "watch")
                self.streams.clear()

    def _fetch_title(self, s):
        try:
            out = subprocess.run(["yt-dlp", "-q", "--no-warnings", "--print", "%(title)s\t%(uploader)s", s["url"]],
                                 capture_output=True, text=True, timeout=60)
            title, _, uploader = out.stdout.strip().partition("\t")
            if title:
                s["title"] = title
                if s["platform"] == "youtube" and uploader:
                    s["channel"] = uploader
                if s["id"] in self.streams:  # don't resurrect a stream that was stopped meanwhile
                    store.watch_add(s)
        except subprocess.TimeoutExpired:
            pass

    def unwatch(self, sid):
        with self.lock:
            st = self.streams.pop(sid, None)
        store.watch_remove(sid)
        if not st:
            return
        st["rec"].stop()
        self.unlisten(sid, "watch")

    # ---- moments ----
    # ---- long highlights + disk guard
    _disk_at, _disk_free = 0.0, 1e9

    def disk(self, cfg=None):
        """Free space on the clips disk (checked every 30 s). Below min_free_gb auto-clipping pauses."""
        now = time.time()
        if now - self._disk_at > 30:
            try:
                self._disk_free = config.disk_free_gb()
            except OSError:
                pass
            self._disk_at = now
        cfg = cfg or config.load()
        lim = cfg.get("min_free_gb", 10)
        return {"free_gb": round(self._disk_free, 1), "min_free_gb": lim, "low": self._disk_free < lim}

    def _kind(self, st, sc, now, cfg, mode=None):
        """short = 12–20 s edit, long = 30–45 s. Auto: long when the action has been going on for a while
        (busy chat most of the last 40 s, or 2+ spikes in 90 s), and at least one long one every few minutes."""
        mode = mode or cfg.get("highlight_length", "auto")
        if mode in ("short", "long", "best"):
            return mode
        recent = list(sc.history)[-20:]
        busy = sum(1 for v in recent if v >= cfg["threshold"] * 0.45) / max(1, len(recent))
        spikes = sum(1 for t in sc.spikes if t > now - 90)
        due = now - st.get("last_long", 0) > cfg.get("long_every_min", 12) * 60
        return "long" if busy >= 0.5 or spikes >= 2 or due else "short"

    @staticmethod
    def _rolls(kind, cfg):
        if kind == "best":
            return cfg.get("best_pre_roll", 75), cfg.get("best_post_roll", 30)
        if kind == "long":
            return cfg.get("long_pre_roll", 50), cfg.get("long_post_roll", 20)
        return cfg["pre_roll"], cfg["post_roll"]

    def _add_moment(self, st, score, tag, reasons, chat, status="clipping", kind="short", at=None, style=None):
        s = st["meta"]
        mid = store.add_moment(stream_id=s["id"], platform=s["platform"], channel=s["channel"], title=s.get("title", ""),
                               url=s["url"], at=at or time.time(), score=round(score, 2), tag=tag, reasons=reasons,
                               chat=chat, status=status, kind=kind)
        st["moments"] += 1
        st.setdefault("clip_times", []).append(time.time())
        if kind in ("long", "best"):
            st["last_long"] = time.time()
        if style:
            store.update_moment(mid, style=style)
        return mid

    def _new_moment(self, st, score, tag, reasons, chat, t0, t1, status="clipping", kind="short", at=None, style=None):
        mid = self._add_moment(st, score, tag, reasons, chat, status, kind, at, style)
        clipper.submit(("clip", mid, st["rec"], t0, t1), prio=0)
        return mid

    def clip_now(self, sid):
        st = self.streams.get(sid)
        if not st:
            raise ValueError("not watching that stream")
        now = time.time()
        cfg = config.load()
        sc = st["score"]
        r = self.rule(st, cfg)
        kind = self._kind(st, sc, now, cfg, r["length"])
        pre, post = self._rolls(kind, cfg)
        return self._new_moment(st, sc.history[-1] if sc.history else 0, "manual", ["clipped by hand"],
                                sc.sample_chat(now), now - pre - post, now + 3, kind=kind, style=r["style"])

    @staticmethod
    def key(st):
        m = st["meta"]
        return store.facecam_key(m["platform"], m["id"], m["channel"])

    def rule(self, st, cfg, rules=None):
        return store.effective_rule(cfg, self.key(st), rules)

    def grab_top_clip(self, c):
        mid = store.add_moment(stream_id=f"twitch:{c['channel'].lower()}", platform="twitch", channel=c["channel"],
                               title=c["title"], url=c["url"], at=time.time(), score=0, tag="topclip",
                               reasons=[f"{c.get('views', 0):,} views on Twitch"], chat=[], status="clipping")
        clipper.submit(("download", mid, c["url"]), prio=0)
        return mid

    # ---- posted -> its video files are deleted 10 s later, unless you press Undo
    def _pending(self):
        if not hasattr(self, "_pend"):
            self._pend = {}
        return self._pend

    def schedule_delete(self, mid, delay=10):
        self.cancel_delete(mid)
        t = threading.Timer(delay, self._do_delete, args=(mid,))
        t.daemon = True
        self._pending()[mid] = (time.time() + delay, t)
        t.start()

    def cancel_delete(self, mid):
        p = self._pending().pop(mid, None)
        if p:
            p[1].cancel()

    def delete_in(self):
        now = time.time()
        return {mid: max(0.0, at - now) for mid, (at, _) in list(self._pending().items())}

    def _do_delete(self, mid):
        self._pending().pop(mid, None)
        m = store.get_moment(mid)
        if m and m["status"] == "posted" and not m.get("cleaned"):
            clipper.delete_clip_files(mid)
            self._disk_at = 0

    def apply_plan(self, mid, merge=None, reset=False):
        """Apply an idea (merged into the clip's edit plan) or reset to the automatic edit, then re-render."""
        import json
        from . import editor
        m = store.get_moment(mid)
        if not m or not m.get("clip"):
            raise ValueError("clip not ready")
        if m.get("cleaned"):
            raise ValueError("this clip's video files were deleted")
        if reset:
            info = dict(m.get("edit_info") or {})
            info["hook_locked"] = False
            r = store.effective_rule(config.load(), store.facecam_key(m["platform"], m["stream_id"], m["channel"]))
            store.update_moment(mid, plan=None, style=r["style"], edit_info=json.dumps(info))
        else:
            add = editor.clean_plan(merge or {}, editor.probe(m["clip"])["dur"])
            if not add:
                raise ValueError("nothing in that idea the editor can use")
            store.update_moment(mid, plan=json.dumps(editor.merge_plan(m.get("plan") or {}, add)))
        clipper.queue_edit(mid)

    def review(self, mid, action, style=None, hook=None, length=None, delete=False):
        m = store.get_moment(mid)
        if not m:
            raise ValueError("no such moment")
        if action in ("keep", "reject"):
            store.update_moment(mid, status="kept" if action == "keep" else "rejected")
        elif action == "posted":
            store.update_moment(mid, status="posted", posted_at=time.time(), asked_at=time.time())
            if delete:
                self.schedule_delete(mid)
        elif action == "unpost":
            self.cancel_delete(mid)
            store.update_moment(mid, status="kept", posted_at=None)
        elif action == "notyet":
            store.update_moment(mid, asked_at=time.time())
        elif action == "vertical":
            if m.get("cleaned"):
                raise ValueError("this clip's video files were deleted")
            if hook is not None:
                info = m.get("edit_info") or {}
                hook = hook.strip().upper()[:60]
                info["hook_locked"] = bool(hook)
                store.update_moment(mid, hook=hook or None, edit_info=__import__("json").dumps(info))
            plan = m.get("plan") or {}
            if plan:  # keep the clip's edit plan in step with what you changed on the card
                if style:
                    plan["style"] = style
                if length in ("short", "long", "best") and length != (m.get("edit_info") or {}).get("kind"):
                    plan["length"] = length
                    plan.pop("start", None)
                    plan.pop("end", None)
                if hook is not None:
                    h = hook.strip().upper()[:60]
                    if h:
                        plan["hook"] = h
                    elif plan.get("hook"):
                        plan.pop("hook")
                store.update_moment(mid, plan=__import__("json").dumps(plan))
            clipper.queue_edit(mid, style, length=length if length in ("short", "long", "best") else None)
        elif action == "reveal":
            path = m.get("vertical") or m.get("clip")
            if path:
                from .osopen import reveal
                reveal(path)
                store.update_moment(mid, opened_at=time.time())  # you probably took it to TikTok: ask later
        else:
            raise ValueError("unknown action")

    # ---- discovery ----
    def refresh_discovery(self):
        self.discovered = discover.discover_all()
        return self.discovered

    def refresh_top_clips(self):
        if not config.has_keys("twitch"):
            self.top_clips = {"clips": [], "note": "Add Twitch keys to .env to see the most-viewed GTA VI clips.",
                              "at": time.time()}
        else:
            try:
                self.top_clips = {"clips": discover.twitch_top_clips(config.load()["game_name"]), "note": "",
                                  "at": time.time()}
            except Exception as e:  # noqa: BLE001
                self.top_clips = {"clips": [], "note": f"Twitch: {e}", "at": time.time()}
        return self.top_clips

    # ---- loop ----
    def _loop(self):
        while True:
            time.sleep(TICK)
            cfg = config.load()
            on = cfg.get("running", True)
            if on != self.running:
                self.set_running(on)
            if not on:
                continue
            rules = store.rules_all()
            for sid, lis in list(self.listeners.items()):
                sc = lis["score"]
                sc.configure(cfg)
                score, parts, tag = sc.tick()
                st = self.streams.get(sid)
                if not st:
                    continue
                r = self.rule(st, cfg, rules)
                st["rule"] = r
                st["last"] = {"score": round(score, 2), "tag": tag, **parts}
                if st["status"] != "live" or not r["auto_clip"] or not r["record"]:
                    continue
                if st["rec"].buffered_seconds() < cfg["pre_roll"]:
                    continue  # not enough video buffered yet for a full clip
                if self.disk(cfg)["low"]:
                    continue  # disk almost full: no new clips until space is freed
                now = time.time()
                if r["max_per_hour"] and sum(1 for t in st.get("clip_times", []) if t > now - 3600) >= r["max_per_hour"]:
                    continue  # this streamer's hourly limit is reached
                kind = self._kind(st, sc, now, cfg, r["length"])
                cooldown = cfg["cooldown"] if kind == "short" else max(cfg["cooldown"], 90)  # long clips don't overlap
                if sc.should_fire(score, threshold=r["threshold"], cooldown=cooldown):
                    pre, post = self._rolls(kind, cfg)
                    self._new_moment(st, score, tag, sc.reasons(parts, tag), sc.sample_chat(now),
                                     now - pre, now + post, kind=kind, style=r["style"])

    def shutdown(self):
        """Stop every recorder and chat reader (called when the server exits)."""
        for st in list(self.streams.values()):
            st["rec"].stop()
        for lis in list(self.listeners.values()):
            if lis["chat"]:
                lis["chat"].stop()

    def snapshot(self):
        out = []
        for sid, st in list(self.streams.items()):
            sc = st["score"]
            out.append({
                **st["meta"], "status": st["status"], "error": st["error"], "since": st["since"],
                "buffered": st["rec"].buffered_seconds(), "has_chat": sc.has_chat,
                "msgs": sc.total_msgs, "last": st["last"], "history": list(sc.history), "moments": st["moments"],
                "clips_hour": sum(1 for t in st.get("clip_times", []) if t > time.time() - 3600),
                "rule": st.get("rule"),
                "stats": sc.stats(),
            })
        return out
