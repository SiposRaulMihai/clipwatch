"""Edit plans from plain words, written by Claude (headless `claude -p` on your Claude plan, a few seconds each).

One request at a time. Claude only sees this clip's facts and transcript and answers with JSON; the editor checks
every field (editor.clean_plan) before using it.
"""
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from . import clipper, config, editor, store

_lock = threading.Lock()
AI_DIR = config.DATA / "ai"

SYSTEM = ("You are the editor inside ClipWatch, a tool that turns GTA 6 livestream moments into vertical 9:16 "
          "TikTok clips. You answer with one JSON object and nothing else: no prose, no code fences.")

FIELDS = """Edit plan fields (all optional; times are seconds in the 16:9 recording):
- "summary": one short line saying what this edit does
- "start", "end": the part of the recording to use
- "remove": [[from, to], ...] parts to cut out
- "jump_cuts": true/false (automatically cut pauses and dead air; default true)
- "length": "short" (12-20 s) | "long" (30-45 s) | "best" (60-90 s) - only when no start/end
- "style": "streamer" (facecam on top, gameplay below) | "reaction" (big facecam) | "bubble" (round facecam over full gameplay) | "cinema" (whole frame on blurred background) | "meme" (white banner with the joke on top)
- "hook": title shown in the first 3 seconds, max 60 characters, ALL CAPS, no emoji ("" = no title)
- "captions": true/false; "caption_style": "pop" (yellow word-by-word) | "box" (green highlight) | "clean" | "meme" (Impact)
- "emoji": one emoji for the reaction sticker at the big moment
- "zooms": [t, ...] extra punch-in zooms
- "texts": [{"at": t, "text": "WAIT FOR IT", "dur": 2}] pop-up text, max 40 characters, no emoji
- "replay": {"at": t, "speed": 0.5} slow-motion instant replay of the moment at t, played right after it"""


def _claude(prompt, cfg):
    exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    if not Path(exe).exists():
        raise RuntimeError("needs Claude Code installed and logged in (see README: Claude features)")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    AI_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-p", prompt, "--model", cfg.get("ai_model", "sonnet"), "--tools", "", "--strict-mcp-config",
           "--no-session-persistence", "--disable-slash-commands", "--output-format", "text",
           "--system-prompt", SYSTEM]
    with _lock:
        r = subprocess.run(cmd, cwd=str(AI_DIR), capture_output=True, text=True, timeout=240, env=env)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1][:200] if (r.stderr or r.stdout).strip()
                           else "Claude didn't answer")
    return r.stdout


def _json(text, want=None):
    """The first complete JSON object in Claude's answer (the first one holding `want`, if given)."""
    first = None
    for m in re.finditer(r"\{", text):
        depth = 0
        for j in range(m.start(), len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[m.start():j + 1])
                    except ValueError:
                        break
                    if want is None or (isinstance(obj, dict) and want in obj):
                        return obj
                    first = first or obj
                    break
    if first is not None and want is None:
        return first
    raise ValueError("Claude's answer couldn't be read")


def _ask_json(prompt, cfg, want=None):
    """Ask, and ask once more if the answer isn't readable JSON (the second try costs a little more usage)."""
    for attempt in range(2):
        reply = _claude(prompt, cfg)
        (AI_DIR / "last_reply.txt").write_text(reply)
        try:
            return _json(reply, want)
        except ValueError:
            if attempt:
                raise
            prompt += "\n\nYour last answer could not be read. Reply with only the JSON object, nothing before or after it."


def _context(m, cfg):
    """What Claude knows about the clip: facts, current edit, loud moments and a timed transcript."""
    src = Path(m["clip"])
    work = AI_DIR / str(m["id"])
    info = editor.probe(src)
    words = editor.full_words(src, m["id"], work, cfg)
    env, hop = editor.audio_envelope(src)
    peak = (m.get("edit_info") or {}).get("peak") or editor.find_peak(env, hop, 0, info["dur"])
    loud = [peak] + editor.extra_peaks(env, hop, 0, info["dur"], [peak], 5, min_gap=3)
    lines, cur = [], []
    for w in words:  # one line per phrase, with its start time
        if cur and (w["s"] - cur[-1]["e"] > 0.8 or len(cur) >= 14):
            lines.append(f"[{cur[0]['s']:.1f}] " + " ".join(x["w"] for x in cur))
            cur = []
        cur.append(w)
    if cur:
        lines.append(f"[{cur[0]['s']:.1f}] " + " ".join(x["w"] for x in cur))
    ei = m.get("edit_info") or {}
    chat = " | ".join(c["text"] for c in (m.get("chat") or [])[:15])
    return info["dur"], (
        f"Streamer: {m['channel']} on {m['platform']}, playing GTA 6. Moment type: {m['tag']} (score {m['score']}).\n"
        f"Why it was clipped: {'; '.join(m.get('reasons') or []) or 'n/a'}\n"
        f"Chat around it: {chat or 'n/a'}\n"
        f"Recording: {info['dur']:.1f} s long.\n"
        f"Current edit: style {m.get('style') or ei.get('style') or 'streamer'}, starts at {ei.get('start', 0)} s, "
        f"{ei.get('length', '?')} s long after {ei.get('cuts', 0)} jump cuts, hook \"{m.get('hook') or ''}\", "
        f"big moment at {peak:.1f} s.\n"
        f"Current edit plan: {json.dumps(m.get('plan') or {})}\n"
        f"Loudest moments (s): {', '.join(f'{t:.1f}' for t in sorted(loud))}\n"
        f"Transcript (seconds in the recording):\n" + ("\n".join(lines) or "(no speech found)")
    )


def plan_from_request(mid, text):
    """'Start at the crash, add a slow-mo replay' -> an edit plan, then a re-render."""
    store.update_moment(mid, ai_state="thinking", ai_request=text[:400], error=None)
    try:
        m = store.get_moment(mid)
        cfg = config.load()
        dur, ctx = _context(m, cfg)
        prompt = (f"{ctx}\n\nThe person posting this clip asks: \"{text}\"\n\n{FIELDS}\n\n"
                  "Write the complete new edit plan for this clip. Do what they ask; keep everything from the current "
                  "plan they didn't ask to change. Use the transcript times. Make it engaging for TikTok: a strong hook "
                  "in the first 2 seconds, no dead air, the payoff easy to see. Reply with only the JSON object.")
        plan = editor.clean_plan(_ask_json(prompt, cfg), dur)
        if not plan:
            raise ValueError("Claude's plan had nothing the editor can use")
        store.update_moment(mid, plan=json.dumps(plan), ai_state=None)
        clipper.queue_edit(mid)
    except Exception as e:  # noqa: BLE001 — show it on the card
        store.update_moment(mid, ai_state="failed", error=f"Claude: {e}"[:300])


def suggest(mid):
    """Three different ways to make this clip more engaging, each a plan you can apply."""
    store.update_moment(mid, ai_state="thinking", error=None)
    try:
        m = store.get_moment(mid)
        cfg = config.load()
        dur, ctx = _context(m, cfg)
        prompt = (f"{ctx}\n\n{FIELDS}\n\nSuggest 3 specific, different ways to make THIS clip more engaging on "
                  "TikTok, based on what actually happens in the transcript. Each idea is a short title (max 6 words), "
                  "one sentence why, and the plan changes it needs. Reply with only: "
                  '{"ideas": [{"title": "...", "why": "...", "plan": {...}}, ...]}')
        raw = _ask_json(prompt, cfg, want="ideas").get("ideas") or []
        ideas = []
        for x in raw[:3]:
            if not isinstance(x, dict):
                continue
            plan = editor.clean_plan(x.get("plan"), dur)
            if plan:
                ideas.append({"title": editor._plain(x.get("title") or "Idea", 60),
                              "why": editor._plain(x.get("why") or "", 160), "plan": plan})
        if not ideas:
            raise ValueError("no usable ideas came back")
        store.update_moment(mid, ai_ideas=json.dumps(ideas), ai_state=None)
    except Exception as e:  # noqa: BLE001
        store.update_moment(mid, ai_state="failed", error=f"Claude: {e}"[:300])
