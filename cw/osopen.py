"""Show a file or folder in the system file manager (macOS, Windows, Linux)."""
import os
import subprocess
import sys
from pathlib import Path


def reveal(path):
    p = Path(path)
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(p)] if p.is_file() else ["open", str(p)])
    elif os.name == "nt":
        subprocess.run(["explorer", f"/select,{p}"] if p.is_file() else ["explorer", str(p)])
    else:
        subprocess.run(["xdg-open", str(p.parent if p.is_file() else p)])
