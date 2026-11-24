"""Live chat readers.

Twitch: the official anonymous read-only chat login (justinfan…), one connection for all channels.
YouTube: yt-dlp writes the live chat as JSON lines; we tail the file.
Kick: not yet (its official API only delivers chat to a public webhook) — audio-only scoring.
"""
import json
import random
import socket
import ssl
import subprocess
import threading
import time

from . import config


class TwitchChat:
    def __init__(self, on_msg):
        self.on_msg = on_msg  # (stream_id, user, text)
        self.channels = set()
        self.sock = None
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True, name="twitch-chat").start()

    def join(self, ch):
        with self.lock:
            self.channels.add(ch)
            self._send(f"JOIN #{ch}")

    def part(self, ch):
        with self.lock:
            self.channels.discard(ch)
            self._send(f"PART #{ch}")

    def _send(self, line):
        if self.sock:
            try:
                self.sock.send((line + "\r\n").encode())
            except OSError:
                pass

    def _connect(self):
        ctx = ssl.create_default_context()
        raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=10)
        s = ctx.wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
        s.send(f"PASS SCHMOOPIIE\r\nNICK justinfan{random.randint(10000, 99999)}\r\n".encode())
        s.settimeout(2)
        self.sock = s
        with self.lock:
            for ch in self.channels:
                self._send(f"JOIN #{ch}")

    def _loop(self):
        buf, backoff = "", 2
        while True:
            if not self.channels:
                time.sleep(1)
                continue
            try:
                if not self.sock:
                    self._connect()
                    backoff = 2
                try:
                    data = self.sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    raise OSError("twitch chat closed")
                buf += data.decode("utf-8", "ignore")
                *lines, buf = buf.split("\r\n")
                for line in lines:
                    if line.startswith("PING"):
                        self._send("PONG :tmi.twitch.tv")
                    elif " PRIVMSG #" in line:
                        user = line[1:line.find("!")] if line.startswith(":") else "?"
                        rest = line.split(" PRIVMSG #", 1)[1]
                        ch, _, text = rest.partition(" :")
                        self.on_msg(f"twitch:{ch}", user, text)
            except OSError:
                self.sock = None
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


def _yt_text(runs):
    out = []
    for r in runs or []:
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            sc = (e.get("shortcuts") or [""])[0]
            out.append(e.get("emojiId") if len(e.get("emojiId", "")) <= 2 else sc)
    return "".join(out)


class YouTubeChat:
    """yt-dlp --write-subs live_chat, tailed line by line."""

    def __init__(self, stream_id, url, on_msg):
        self.sid, self.url, self.on_msg = stream_id, url, on_msg
        self.dir = config.BUFFER / (stream_id.replace(":", "_").replace("@", "") + "_chat")
        self.stop_ev = threading.Event()
        self.proc = None
        threading.Thread(target=self._run, daemon=True, name=f"ytchat-{stream_id}").start()

    def stop(self):
        self.stop_ev.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _run(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        for old in self.dir.glob("chat*"):
            old.unlink(missing_ok=True)
        while not self.stop_ev.is_set():
            self.proc = subprocess.Popen(
                ["yt-dlp", "-q", "--no-warnings", "--match-filter", "live_status=is_live",
                 "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                 "-o", str(self.dir / "chat.%(ext)s"), self.url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            path = self.dir / "chat.live_chat.json.part"
            pos, first, started = 0, True, time.time()
            while not self.stop_ev.wait(1) and self.proc.poll() is None:
                if not path.exists():
                    continue
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                nl = chunk.rfind(b"\n")
                if nl < 0:
                    continue
                pos += nl + 1
                # the first batch is backlog from before we joined — skip it
                if first and time.time() - started < 8:
                    continue
                first = False
                for raw in chunk[:nl].splitlines():
                    self._parse(raw)
            if self.stop_ev.wait(10):
                break
            pos = 0

    def _parse(self, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        acts = (d.get("replayChatItemAction") or {}).get("actions") or []
        for a in acts:
            item = (a.get("addChatItemAction") or {}).get("item") or {}
            r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if not r:
                continue
            text = _yt_text((r.get("message") or {}).get("runs"))
            user = (r.get("authorName") or {}).get("simpleText", "?")
            if text:
                self.on_msg(self.sid, user, text)
