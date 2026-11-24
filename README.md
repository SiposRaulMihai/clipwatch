# ClipWatch

**Watch live streams, catch the moments chat goes crazy, and get TikTok-ready 9:16 clips automatically.**
Built for GTA 6 streams on Twitch, YouTube and Kick. It runs entirely on your own computer, and it's free and open source.

Design & made by **[siposraulmihai.xyz](https://siposraulmihai.xyz)**

---

## What it does

- **Finds streams.** A Scout ranks live streamers by laughs in chat, chat spikes, chat per 1K viewers, viewers and Twitch clips. **Autopilot** records the best ones and swaps in better streams as they go live.
- **Watches chat and sound.** Every stream gets a live *moment score* built from chat speed against that stream's own normal, laughing (`LMAO`, `😂`, `KEKW`…), hype, "clip it" calls and loudness spikes.
- **Clips only the real moments.** A clip is made when a stream's score crosses its line. You set that line per streamer on the **Rules board**.
- **Edits to 9:16 by itself:**
  - Layouts: facecam on top with gameplay below, big reaction, facecam bubble, cinematic, or a meme banner.
  - Word-by-word captions (on-device Whisper), a hook title, and zoom punch-ins with shake and flash.
  - Jump cuts that remove dead air, a reaction emoji, a progress bar and a credit for the streamer.
- **Three lengths:** Short (12–20 s), Long (30–45 s) and Best (60–90 s), plus Auto, which picks by how long the action lasts.
- **Facecam box.** If the automatic webcam crop is wrong, draw the webcam once for that streamer and every clip of theirs uses it.
- **Edit ideas.** Each clip suggests edits (slow-mo instant replay, a "WAIT FOR IT" tease, a hook from what the streamer actually said, and more) that you apply with one click.
- **Tell Claude how to edit it** (optional). Write *"start at the crash, slow-mo replay, hook BRO WHAT"* and the clip re-renders.
- **Posting workflow.** Mark clips as posted and their video files are deleted to free space, with Undo. A disk guard pauses clipping before your disk fills up.
- **Compilations.** One command joins the best short clips into 2-minute funny compilations with an animated title card and transitions.
- **Master On/Off switch** for everything. Clips are saved per streamer: `ClipWatch/clips/<streamer>/<date>/`.

## Requirements

| | |
|---|---|
| **Best on** | macOS 12+ (Apple Silicon or Intel): hardware video encoding and automatic webcam detection |
| **Also works** | Linux. Windows works too (WSL is easiest). There's no automatic webcam detection on either, so draw the facecam box per streamer, or use the Cinematic/Meme styles |
| **You need** | Python 3.10+, ffmpeg 4.3+, about 2 GB for the speech model and software, and free disk for clips (each clip is about 30–100 MB) |
| **Internet** | about 6–8 Mbps per stream you record at 1080p |

## Install

### macOS / Linux

```bash
git clone https://github.com/SiposRaulMihai/clipwatch.git
cd clipwatch
./setup.sh
./run.sh
```

`setup.sh` installs ffmpeg (with Homebrew or apt if it's missing), creates a Python virtualenv and installs the packages. `run.sh` starts ClipWatch and opens **http://127.0.0.1:8792**. Stop it with `Ctrl+C`.

On a Mac, run `xcode-select --install` once if you don't have the Xcode command line tools. ClipWatch uses them to find streamers' webcams.

### Windows

1. Install [Python 3.12](https://www.python.org/downloads/) (tick *Add to PATH*) and ffmpeg (`winget install Gyan.FFmpeg`).
2. In the `clipwatch` folder:
   ```bat
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   copy .env.example .env
   ```
3. Double-click **run.bat**.

### Update

```bash
git pull && ./setup.sh
```

Your settings (`config.json`), keys (`.env`) and clips are never touched by updates.

## First run

1. Open the dashboard and flip the **On/Off** switch in the top bar to **On**.
2. Turn on **Autopilot**, or paste any Twitch, YouTube or Kick link in the **Live now** tab and press *Watch*.
3. Set a score line per streamer on the **Rules board**: higher means fewer, bigger moments. The *All streamers* row sets the defaults.
4. Clips show up under **Moments**. Keep, reject, re-render in another style or length, or press **Posted** after you upload one.
5. Click **Clips folder** to open them. They're saved in `~/ClipWatch/clips/` (change `output_dir` in `config.json`).

The first clip takes a little longer while the speech model (about 460 MB) downloads once.

## Optional keys

ClipWatch works without any keys: YouTube via yt-dlp, Twitch chat via the anonymous read-only login, and Kick by sound only. Keys add viewer counts, Twitch clip stats and the *Top clips* tab:

- **Twitch:** create an app at [dev.twitch.tv/console](https://dev.twitch.tv/console). Set the client type to **Confidential** and the redirect URL to `https://localhost`. Put the Client ID and a new Client Secret in `.env`.
- **Kick:** create an app at [kick.com/settings/developer](https://kick.com/settings/developer) and put its keys in `.env`.

`.env` stays on your computer. It's in `.gitignore`.

## Claude features (optional)

*Tell Claude how to edit it* and *More ideas from Claude* use [Claude Code](https://claude.com/claude-code) on your own Claude plan:

```bash
npm install -g @anthropic-ai/claude-code
claude   # log in once
```

Each request takes about 15–25 s and uses a little of your plan. The model is `ai_model` in `config.json` (default `sonnet`). Everything else in ClipWatch works without Claude: the free edit ideas, captions and editing are all local.

## Compilations

```bash
.venv/bin/python tools/compile.py --count 5 --per 8 --out ~/Desktop/GTA6/Compilations
```

This makes 5 compilations of 8 short clips each, funniest and kept clips first, never the same streamer twice in a row. Each has an animated title card, a different transition between clips, and a `.txt` listing the streamers for your caption. Clips already used are skipped next time.

## Main settings (`config.json`)

Most of these can be changed in the dashboard.

| Key | Default | What it does |
|---|---|---|
| `running` | `false` | master switch |
| `max_streams` | `3` | streams recorded at once |
| `threshold` | `6` | default score line (Rules board can override per streamer) |
| `highlight_length` | `best` | `short` / `long` / `best` / `auto` |
| `edit_style` | `streamer` | `streamer`, `reaction`, `bubble`, `cinema`, `meme` |
| `output_dir` | `~/ClipWatch/clips` | where clips are saved |
| `min_free_gb` | `10` | clipping pauses below this much free disk |
| `favorites` | US/UK GTA streamers | always checked by the Scout |
| `english_only`, `gta_only` | `true` | Scout filters |
| `whisper_model` | `small` | `base` is faster, `medium` is more accurate |
| `port` | `8792` | dashboard port |

## How it works

```
streamlink / yt-dlp ─► ffmpeg ring buffer (5 s chunks, 7 min)    chat (Twitch IRC, YouTube live chat)
                                  │                                        │
                                  └──────► moment score per stream ◄───────┘ + loudness
                                                   │ crosses the streamer's line
                                                   ▼
                          clip cut from the buffer ─► 9:16 editor (whisper captions, facecam,
                                                       jump cuts, hook, zooms) ─► clips/<streamer>/<date>/
```

| Path | What's in it |
|---|---|
| `server.py` | local web server and API (127.0.0.1 only) |
| `cw/engine.py` | streams, scoring, rules, master switch |
| `cw/scout.py` | finding and ranking streamers, autopilot |
| `cw/recorder.py` | recording |
| `cw/chat.py` | chat readers |
| `cw/detector.py` | moment score |
| `cw/clipper.py` | cutting clips, job queue |
| `cw/editor.py` | the 9:16 editor |
| `cw/ai.py` | Claude features |
| `cw/native/facedetect.swift` | Apple Vision webcam finder |
| `ui/` | the dashboard |
| `data/` | database and rolling buffer, local only |

## Use it responsibly

Clips belong to the streamers who made them. Credit them (ClipWatch puts a credit on every edit), follow the rules of TikTok, YouTube, Twitch and Kick, and respect a creator who doesn't want their streams clipped. ClipWatch isn't affiliated with Rockstar Games, Twitch, YouTube, Kick or TikTok.

## License

MIT: free to use, change and share. See [LICENSE](LICENSE).

---

Design & made by **[siposraulmihai.xyz](https://siposraulmihai.xyz)** · ⭐ Star the repo if ClipWatch helps you.
