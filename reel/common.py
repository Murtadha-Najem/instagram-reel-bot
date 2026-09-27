import json
import os
import re
import subprocess
import sys
from pathlib import Path

PY = sys.executable
ROOT = Path(__file__).resolve().parent.parent
# The permanent store for every post's video, frames, metadata and transcripts, not a disposable cache.
# The bot sets these through the environment from config.toml; the defaults suit running reel.py by hand.
DATA = Path(os.environ.get("REEL_DATA") or ROOT / "data")
CACHE_ROOT = Path(os.environ.get("REEL_CACHE") or DATA / "cache")
RECORDS = Path(os.environ.get("REEL_RECORDS") or DATA / "records")
COOKIES = Path(os.environ.get("REEL_COOKIES") or Path.home() / ".config" / "reel" / "cookies.txt")
PTS_RE = re.compile(r"pts_time:([0-9.]+)")


class ReelError(Exception):
    """A failure with a message the user can act on."""


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def run_bytes(cmd):
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise ReelError(f"{cmd[0]} failed: {p.stderr.decode('utf-8', 'replace')[-400:]}")
    return p.stdout


def probe(path):
    p = run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if p.returncode != 0:
        raise ReelError(f"ffprobe failed on {path}")
    data = json.loads(p.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "duration": float(data.get("format", {}).get("duration") or v.get("duration") or 0),
        "width": v.get("width"),
        "height": v.get("height"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def fmt_time(seconds):
    total = int(round(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def ffmpeg_times(stderr):
    return [float(m.group(1)) for m in PTS_RE.finditer(stderr)]
