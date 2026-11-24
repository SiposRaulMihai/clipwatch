"""Find live GTA 6 streams.

YouTube: yt-dlp search (no keys). Twitch and Kick: their official APIs (keys in .env).
"""
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

from . import config

UA = "ClipWatch/1.0 (local stream clipper)"
_tokens = {}


def parse_url(url):
    """Return (platform, channel, stream_id) for a pasted stream URL."""
    url = url.strip()
    u = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in u.path.split("/") if p]
    if host.endswith("twitch.tv") and parts:
        ch = parts[0].lower()
        return "twitch", ch, f"twitch:{ch}"
    if host.endswith("kick.com") and parts:
        ch = parts[0].lower()
        return "kick", ch, f"kick:{ch}"
    if host in ("youtube.com", "youtu.be"):
        vid = None
        if host == "youtu.be" and parts:
            vid = parts[0]
        elif parts[:1] == ["watch"]:
            vid = urllib.parse.parse_qs(u.query).get("v", [None])[0]
        elif parts[:1] in (["live"], ["shorts"]) and len(parts) > 1:
            vid = parts[1]
        if vid:
            return "youtube", vid, f"youtube:{vid}"
        if parts and parts[0].startswith("@"):
            return "youtube", parts[0], f"youtube:{parts[0]}"
    raise ValueError("Paste a YouTube, Twitch or Kick stream link")


def canonical_url(platform, channel):
    if platform == "twitch":
        return f"https://www.twitch.tv/{channel}"
    if platform == "kick":
        return f"https://kick.com/{channel}"
    if channel.startswith("@"):
        return f"https://www.youtube.com/{channel}/live"
    return f"https://www.youtube.com/watch?v={channel}"


def _http(url, data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())



GTA_RE = re.compile(r"\bgta\b|gta ?(6|vi)\b|grand theft auto|vice city|leonida", re.I)
NON_LATIN = re.compile(r"[\u0400-\u04FF\u0590-\u06FF\u0900-\u0DFF\u0E00-\u0E7F\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF]")
FOREIGN = {
    "jucam", "noul", "din", "pe", "cu", "si", "și", "prima", "jocul", "khelne", "wapas", "agayi", "aaj", "hai",
    "bhai", "jugando", "vivo", "directo", "jogando", "ao", "partie", "oynuyoruz", "canlı", "spielen", "zocken",
    "gramy", "na", "żywo", "hraju", "live\u00e9", "tiempo", "nuevo", "novo", "juego", "jogo", "en", "el", "la", "de",
}


def looks_english(title):
    if NON_LATIN.search(title or ""):
        return False
    words = re.findall(r"[a-zà-ÿăâîșț']+", (title or "").lower())
    foreign = sum(w in FOREIGN for w in words)
    return foreign < 2 and not (words and foreign / len(words) > 0.25)


def is_gta(*texts):
    return any(GTA_RE.search(t or "") for t in texts)

# ---------- YouTube ----------

def youtube_live(query, n=50):
    out = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--no-warnings", "-J", f"ytsearch{n}:{query}"],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-300:] or "yt-dlp search failed")
    entries = json.loads(out.stdout).get("entries") or []
    res = []
    for e in entries:
        if e.get("live_status") != "is_live":
            continue
        vid = e.get("id")
        thumbs = e.get("thumbnails") or []
        res.append({
            "platform": "youtube",
            "channel": vid,
            "display": e.get("channel") or e.get("uploader") or "YouTube",
            "title": e.get("title") or "",
            "viewers": e.get("concurrent_view_count") or 0,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumb": thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        })
    return res


# ---------- Twitch (Helix, official) ----------

def _twitch_token():
    tok = _tokens.get("twitch")
    if tok and tok[1] > time.time() + 60:
        return tok[0]
    q = urllib.parse.urlencode({
        "client_id": os.environ["TWITCH_CLIENT_ID"],
        "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
        "grant_type": "client_credentials",
    }).encode()
    d = _http("https://id.twitch.tv/oauth2/token", data=q)
    _tokens["twitch"] = (d["access_token"], time.time() + d.get("expires_in", 3600))
    return d["access_token"]


def _helix(path, **params):
    h = {"Client-Id": os.environ["TWITCH_CLIENT_ID"], "Authorization": f"Bearer {_twitch_token()}"}
    return _http(f"https://api.twitch.tv/helix/{path}?{urllib.parse.urlencode(params)}", headers=h)


def _twitch_game_id(name):
    data = _helix("games", name=name).get("data") or []
    if not data:
        raise RuntimeError(f"Twitch has no category named {name!r}")
    return data[0]["id"]


def twitch_live(game, n=30):
    gid = _twitch_game_id(game)
    res = []
    lang = config.load().get("languages", ["en"])
    params = {"game_id": gid, "first": n}
    if lang:
        params["language"] = lang[0]
    for s in _helix("streams", **params).get("data", []):
        res.append({
            "platform": "twitch",
            "user_id": s["user_id"],
            "channel": s["user_login"],
            "display": s["user_name"],
            "title": s["title"],
            "viewers": s["viewer_count"],
            "url": f"https://www.twitch.tv/{s['user_login']}",
            "thumb": s["thumbnail_url"].replace("{width}", "440").replace("{height}", "248"),
            "lang": s.get("language"),
        })
    return res


def twitch_top_clips(game, hours=24, n=30):
    gid = _twitch_game_id(game)
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
    res = []
    for c in _helix("clips", game_id=gid, started_at=since, first=n).get("data", []):
        res.append({
            "platform": "twitch",
            "channel": c["broadcaster_name"],
            "title": c["title"],
            "views": c["view_count"],
            "duration": c["duration"],
            "url": c["url"],
            "thumb": c["thumbnail_url"],
            "created": c["created_at"],
        })
    return res


# ---------- Kick (official public API) ----------

def _kick_token():
    tok = _tokens.get("kick")
    if tok and tok[1] > time.time() + 60:
        return tok[0]
    q = urllib.parse.urlencode({
        "client_id": os.environ["KICK_CLIENT_ID"],
        "client_secret": os.environ["KICK_CLIENT_SECRET"],
        "grant_type": "client_credentials",
    }).encode()
    d = _http("https://id.kick.com/oauth/token", data=q,
              headers={"Content-Type": "application/x-www-form-urlencoded"})
    _tokens["kick"] = (d["access_token"], time.time() + d.get("expires_in", 3600))
    return d["access_token"]


def _kick(path, **params):
    h = {"Authorization": f"Bearer {_kick_token()}", "Accept": "application/json"}
    return _http(f"https://api.kick.com/public/v1/{path}?{urllib.parse.urlencode(params)}", headers=h)


def kick_live(category_query, n=30):
    cats = _kick("categories", q=category_query).get("data") or []
    want = re.compile(r"grand theft auto|gta", re.I)
    cats = [c for c in cats if want.search(c.get("name", ""))] or cats
    if not cats:
        raise RuntimeError("Kick returned no GTA category")
    res = []
    for c in cats[:2]:
        lang = (config.load().get("languages") or [None])[0]
        params = {"category_id": c["id"], "limit": n, "sort": "viewer_count"}
        if lang:
            params["language"] = lang
        for s in _kick("livestreams", **params).get("data", []):
            slug = s.get("slug") or s.get("channel_slug") or ""
            if not slug:
                continue
            res.append({
                "platform": "kick",
                "channel": slug,
                "display": slug,
                "title": s.get("stream_title", ""),
                "viewers": s.get("viewer_count", 0),
                "url": f"https://kick.com/{slug}",
                "thumb": s.get("thumbnail") or "",
                "category": c.get("name"),
            })
    return res



def twitch_clip_stats(user_id, hours=24):
    """How many clips viewers made of this streamer lately, and how many views they got."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
    data = _helix("clips", broadcaster_id=user_id, started_at=since, first=50).get("data", [])
    return {"clips": len(data), "clip_views": sum(c["view_count"] for c in data)}


def _streamlink_meta(url):
    sl = str(config.ROOT / ".venv" / "bin" / "streamlink")
    out = subprocess.run([sl, "--json", url], capture_output=True, text=True, timeout=45)
    try:
        d = json.loads(out.stdout or "{}")
    except ValueError:
        return None
    if "error" in d or not d.get("streams"):
        return None
    return d.get("metadata") or {}


def check_favorite(f):
    """Is this favorite live right now? Returns a candidate dict or None."""
    p, ch = f["platform"], f["channel"]
    try:
        if p == "youtube":
            url = f"https://www.youtube.com/{ch if ch.startswith('@') else '@' + ch}/live"
            out = subprocess.run(["yt-dlp", "-q", "--no-warnings", "--print",
                                  "%(id)s\t%(live_status)s\t%(concurrent_view_count)s\t%(title)s", url],
                                 capture_output=True, text=True, timeout=60)
            vid, status, viewers, title = (out.stdout.strip().split("\t") + ["", "", "", ""])[:4]
            if status != "is_live":
                return None
            return {"platform": "youtube", "channel": vid, "display": f["name"], "title": title,
                    "viewers": int(viewers) if viewers.isdigit() else 0,
                    "url": f"https://www.youtube.com/watch?v={vid}", "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}
        url = canonical_url(p, ch)
        meta = _streamlink_meta(url)
        if meta is None:
            return None
        return {"platform": p, "channel": ch.lower(), "display": f["name"], "title": meta.get("title") or "",
                "category": meta.get("category") or "", "viewers": 0, "url": url, "thumb": ""}
    except (subprocess.TimeoutExpired, OSError):
        return None


def check_favorites(favs, workers=5):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(check_favorite, favs))
    out = []
    for f, r in zip(favs, res):
        if r:
            r.update({"fav": True, "country": f.get("country", "")})
            out.append(r)
    return out


def discover_all():
    cfg = config.load()
    found, notes = [], []
    jobs = [("youtube", lambda: youtube_live(cfg["youtube_query"]))]
    if config.has_keys("twitch"):
        jobs.append(("twitch", lambda: twitch_live(cfg["game_name"])))
    else:
        notes.append("Twitch: add keys to .env to list live GTA VI streams (or paste links).")
    if config.has_keys("kick"):
        jobs.append(("kick", lambda: kick_live(cfg["kick_category_query"])))
    else:
        notes.append("Kick: add keys to .env to list live GTA streams (or paste links).")
    for name, fn in jobs:
        try:
            found += fn()
        except Exception as e:  # noqa: BLE001 — report per platform, keep the others
            notes.append(f"{name}: {e}")
    if cfg.get("english_only", True):
        before = len(found)
        found = [x for x in found if x["platform"] != "youtube" or looks_english(x["title"])]
        if before - len(found):
            notes.append(f"Hid {before - len(found)} non-English YouTube streams (english_only).")
    found.sort(key=lambda s: s.get("viewers") or 0, reverse=True)
    return {"streams": found, "notes": notes, "at": time.time()}
