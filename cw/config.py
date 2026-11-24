"""Settings, paths and the .env loader."""
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BUFFER = DATA / "buffer"
CONFIG_FILE = ROOT / "config.json"           # your settings (created on first run, not in git)
DEFAULTS_FILE = ROOT / "config.default.json"  # shipped defaults

_lock = threading.Lock()


def _ensure():
    """First run: copy the defaults. After an update: add settings that are new in the defaults."""
    defaults = json.loads(DEFAULTS_FILE.read_text())
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(defaults, indent=2, ensure_ascii=False) + "\n")
        return
    cfg = json.loads(CONFIG_FILE.read_text())
    missing = {k: v for k, v in defaults.items() if k not in cfg}
    if missing:
        CONFIG_FILE.write_text(json.dumps({**cfg, **missing}, indent=2, ensure_ascii=False) + "\n")


def load_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if v.strip():
            os.environ.setdefault(k.strip(), v.strip())


def load():
    with _lock:
        return json.loads(CONFIG_FILE.read_text())


def save(patch):
    """Merge a partial dict into config.json (only keys that already exist)."""
    with _lock:
        cfg = json.loads(CONFIG_FILE.read_text())
        for k, v in patch.items():
            if k not in cfg:
                continue
            old = cfg[k]
            num = (int, float)
            if isinstance(old, bool) or isinstance(v, bool):
                ok = isinstance(old, bool) and isinstance(v, bool)
            elif isinstance(old, num):
                ok = isinstance(v, num)
            else:
                ok = isinstance(v, type(old))
            if ok:
                cfg[k] = v
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
        return cfg


def disk_free_gb():
    """Free space (GB) on the disk the clips are written to."""
    import shutil
    out = output_dir()
    out.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(out).free / 1e9


def output_dir():
    return Path(os.path.expanduser(load()["output_dir"]))


def has_keys(platform):
    if platform == "twitch":
        return bool(os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET"))
    if platform == "kick":
        return bool(os.environ.get("KICK_CLIENT_ID") and os.environ.get("KICK_CLIENT_SECRET"))
    return True


DATA.mkdir(exist_ok=True)
BUFFER.mkdir(exist_ok=True)
_ensure()
load_env()
