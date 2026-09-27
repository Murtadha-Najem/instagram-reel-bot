"""Turn an Instagram reel or post link into a compact bundle an agent can read.

usage: python reel.py <instagram url> [--force-transcribe] [--refresh] [--max-frames N] [--dense]

The bot calls this for every shared post; it also works on its own.
"""
import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from reel.common import ReelError  # noqa: E402
from reel.pipeline import process  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--force-transcribe", action="store_true", help="transcribe even if captions cover the speech")
    ap.add_argument("--refresh", action="store_true", help="download again instead of using the cache")
    ap.add_argument("--max-frames", type=int, default=8)
    ap.add_argument("--dense", action="store_true",
                    help="send every visually distinct frame instead of one per camera shot (for product reveals, props, gestures)")
    args = ap.parse_args()
    result = process(args.url, force_transcribe=args.force_transcribe, refresh=args.refresh,
                     max_frames=args.max_frames, dense=args.dense)
    print(result["bundle"])


if __name__ == "__main__":
    try:
        main()
    except ReelError as e:
        print(f"REEL ERROR: {e}", file=sys.stderr)
        sys.exit(1)
