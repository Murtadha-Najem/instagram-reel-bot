"""Every frame at a low rate, contact sheets of any moments, and full frames on demand.

The pipeline no longer decides which frames Claude sees. It keeps the whole video browsable: one overview sheet
built from the detected moments, and commands (look.py) to open a sheet of any time range or a single full frame,
so Claude can move forward, back, zoom in or stop once the post is understood.
"""
from pathlib import Path

from .common import ReelError, run

ALL_FPS = 2            # every half second is enough to follow a gag or a caption; a 90 s reel is 180 small files
ALL_WIDTH = 360
CELL_W = 180           # a 4 x 4 sheet of 9:16 cells is 720 x ~1370 px, about 1,300 Claude image tokens for 16 moments
SHEET_COLS = 4
LABEL_H = 20
FULL_WIDTH = 720       # full frames are for reading text


def all_dir(workdir):
    return Path(workdir) / "all"


def look_dir(workdir):
    d = Path(workdir) / "look"
    d.mkdir(exist_ok=True)
    return d


def fmt_precise(t):
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def extract_all(video, workdir):
    d = all_dir(workdir)
    d.mkdir(exist_ok=True)
    for old in d.glob("*.jpg"):
        old.unlink()
    p = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-vf",
             f"fps={ALL_FPS},scale={ALL_WIDTH}:-2", "-q:v", "5", str(d / "f_%05d.jpg")])
    count = len(list(d.glob("f_*.jpg")))
    if not count:
        raise ReelError("could not extract frames: " + p.stderr[-300:])
    return count


def nearest(workdir, t):
    files = sorted(all_dir(workdir).glob("f_*.jpg"))
    if not files:
        return None
    return files[min(max(int(round(t * ALL_FPS)), 0), len(files) - 1)]


def even_times(start, end, n):
    if n <= 1 or end <= start:
        return [start]
    return [round(start + i * (end - start) / (n - 1), 2) for i in range(n)]


def make_sheet(workdir, cells, out):
    """cells: [(seconds, label)]. One grid image, the time and label printed under each cell."""
    from PIL import Image, ImageDraw, ImageFont
    images = []
    for t, label in cells:
        p = nearest(workdir, t)
        if p is None:
            continue
        with Image.open(p) as im:
            im = im.convert("RGB")
            images.append((im.resize((CELL_W, round(im.height * CELL_W / im.width))), f"{fmt_precise(t)} {label}".strip()))
    if not images:
        raise ReelError("no frames available for a sheet")
    cell_h = max(im.height for im, _ in images)
    rows = (len(images) + SHEET_COLS - 1) // SHEET_COLS
    sheet = Image.new("RGB", (SHEET_COLS * CELL_W, rows * (cell_h + LABEL_H)), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    for i, (im, text) in enumerate(images):
        r, c = divmod(i, SHEET_COLS)
        x, y = c * CELL_W, r * (cell_h + LABEL_H)
        sheet.paste(im, (x, y))
        draw.text((x + 3, y + cell_h + 3), text, fill="black", font=font)
    sheet.save(out, quality=85)
    return out


def full_frame(video, workdir, t):
    out = look_dir(workdir) / f"full_{int(t * 10):05d}.jpg"
    if not out.exists():
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale={FULL_WIDTH}:-2", "-q:v", "3", str(out)])
    if not out.exists():
        raise ReelError(f"could not extract a frame at {t:.2f}s")
    return out
