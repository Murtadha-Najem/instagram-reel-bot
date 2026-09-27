"""Look further into a processed post: a contact sheet of any time range, or one full frame.

usage:
  python look.py <shortcode> sheet [--start S] [--end E] [--n 12]
  python look.py <shortcode> frame <seconds>
Prints the image path to open with Read (and, for sheets, the time of each cell).
"""
import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from reel import frames  # noqa: E402
from reel.common import CACHE_ROOT, ReelError, probe  # noqa: E402

MAX_CELLS = 24


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("shortcode")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet", help="contact sheet of evenly spaced moments in a time range")
    s.add_argument("--start", type=float, default=0.0)
    s.add_argument("--end", type=float)
    s.add_argument("--n", type=int, default=12)
    f = sub.add_parser("frame", help="one full-resolution frame, for reading text")
    f.add_argument("at", type=float)
    args = ap.parse_args()

    workdir = CACHE_ROOT / args.shortcode
    video = workdir / "video.mp4"
    if not video.exists():
        raise ReelError(f"no processed video for {args.shortcode}; run reel.py on its link first")
    duration = probe(video)["duration"]
    if not any(frames.all_dir(workdir).glob("f_*.jpg")):
        frames.extract_all(video, workdir)

    if args.cmd == "sheet":
        end = min(args.end if args.end is not None else duration, duration)
        start = max(0.0, min(args.start, end))
        n = max(1, min(args.n, MAX_CELLS))
        times = frames.even_times(start, end, n)
        out = frames.look_dir(workdir) / f"sheet_{int(start * 10):05d}_{int(end * 10):05d}_{n}.jpg"
        frames.make_sheet(workdir, [(t, "") for t in times], out)
        print(out)
        print("cells: " + ", ".join(frames.fmt_precise(t) for t in times))
    else:
        t = max(0.0, min(args.at, max(duration - 0.1, 0.0)))
        print(frames.full_frame(video, workdir, t))


if __name__ == "__main__":
    try:
        main()
    except ReelError as e:
        print(f"LOOK ERROR: {e}", file=sys.stderr)
        sys.exit(1)
