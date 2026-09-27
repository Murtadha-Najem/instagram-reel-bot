"""Write the compact bundle Claude reads: text first, then an overview sheet and a timeline to navigate by."""
import json

from .audio import SINGING_MIN
from .common import RECORDS, fmt_time

# Already shown elsewhere in the bundle, or not useful to a reader.
SKIP_DETAILS = {"shortcode", "media_id", "repost_notes"}


def record_path(meta):
    account = meta["account"] or "unknown"
    return RECORDS / f"{meta['date'] or 'undated'}_{account}_{meta['shortcode']}.md"


def _when(item):
    return item.get("label") or fmt_time(item["t"])


def _value(v):
    if isinstance(v, dict):
        return ", ".join(f"{k}: {x}" for k, x in v.items())
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def _spans(windows, test):
    """Consecutive audio windows passing `test`, merged into 'MM:SS-MM:SS' spans."""
    spans = []
    for w in windows:
        if not test(w):
            continue
        if spans and w["start"] - spans[-1][1] < 0.5:
            spans[-1][1] = w["end"]
        else:
            spans.append([w["start"], w["end"]])
    return ", ".join(f"{fmt_time(s)}-{fmt_time(e)}" for s, e in spans) or "none"


def _times(values):
    return ", ".join(fmt_time(t) for t in values) or "none"


def write(meta, duration, video, audio, speech, decisions):
    details = meta.get("details") or {}
    code = meta["shortcode"]
    counts = " / ".join(f"{n} {k}" for k, n in (("likes", meta["likes"]), ("views", meta["views"]),
                                                ("comments", meta["comments"])) if n is not None)
    if details.get("counts_hidden_by_owner"):
        # With counts hidden, Instagram returns a token like count (3 on a reel with 201k plays).
        counts += " (owner hides like counts: the like figure is not real)"
    fmt = f"photo post, {len(meta['images'])} image(s)" if meta["video"] is None else f"video, {duration:.1f}s"
    L = [f"# Instagram {code}", "",
         f"- url: {meta['url']}",
         f"- account: @{meta['account']} ({meta['name']})",
         f"- posted: {meta['date']}",
         f"- format: {fmt}",
         f"- engagement: {counts or 'not returned'}",
         f"- record file to write: {record_path(meta)}"]
    for note in details.get("repost_notes") or []:
        L.append(f"- repost note by @{note.get('by')} ({note.get('at_utc')}): {note.get('text') or '(no text)'}")

    L += ["", "## Caption (may be unrelated bait; judge it)", "", meta["caption"] or "(none)", ""]

    if details:
        L += ["## Instagram metadata (full response in media_raw.json)", ""]
        L += [f"- {k}: {_value(v)}" for k, v in details.items() if k not in SKIP_DETAILS]
        L.append("")

    L += ["## Audio", "", f"- kind: {audio['kind']}"]
    song = audio.get("song")
    if song:
        certainty = "" if song.get("confirmed", True) else " (single window match, treat as possible)"
        L.append(f"- song: {song['title']} by {song['artist']} [{song['source']}]{certainty}")
    elif meta.get("music"):
        m = meta["music"]
        L.append(f"- Instagram audio: {m['type']} \"{m['title']}\" by {m['artist']}")

    L += ["", f"## Speech (source: {speech['source']})", ""]
    if speech.get("language"):
        L.append(f"language: {speech['language']}")
    L += [f"[{s['t']}] {s['text']}" for s in speech.get("segments", [])] or ["(none)"]

    L += ["", "## On-screen text (local OCR, may contain misreads)", ""]
    L += [f"[{_when(o)}] ({o['band']}) {o['text']}" for o in video["onscreen"]] or ["(none)"]

    overview = video.get("overview")
    if overview:
        timeline = video.get("timeline") or {}
        windows = audio.get("windows") or []
        L += ["", "## Overview sheet (open this first)", "", f"- {overview['path']}"]
        L += [f"- cell {i}: [{fmt_time(c['t'])}] {c['reason']}" for i, c in enumerate(overview["cells"], 1)]
        L += ["", "## Timeline (detected by the pipeline: hints for where to look, not conclusions)", "",
              f"- duration {duration:.1f}s; {video['static_ratio']:.0%} of it has no movement",
              f"- cuts: {_times(timeline.get('cuts', []))}",
              f"- text changes in the top or bottom strip: {_times(timeline.get('text_changes', []))}"]
        for name, times in (timeline.get("busy") or {}).items():
            L.append(f"- {name} strip changes constantly ({len(times)} changes, {fmt_time(times[0])} to "
                     f"{fmt_time(times[-1])}): camera motion, animation, or fast-changing text")
        if windows:
            L.append(f"- speech: {_spans(windows, lambda w: w['is_speech'])}")
            L.append(f"- singing: {_spans(windows, lambda w: w['singing'] >= SINGING_MIN)}")
        L += ["", "## Look further", "",
              f"- sheet of a time range: `python look.py {code} sheet --start S --end E --n N` (N up to 24)",
              f"- one full frame, for reading text: `python look.py {code} frame SECONDS`"]
    else:
        L += ["", f"## Frames to read ({len(video['frames'])})", ""]
        L += [f"- [{_when(f)}] {f['reason']}: {f['path']}" for f in video["frames"]]

    L += ["", "## Decisions", ""] + [f"- {d}" for d in decisions]

    out = meta["dir"] / "bundle.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    (meta["dir"] / "bundle.json").write_text(json.dumps(
        {"meta": {k: v for k, v in meta.items() if k not in ("dir", "video", "images")}, "duration": duration,
         "video": video, "audio": {k: v for k, v in audio.items() if k != "windows"},
         "speech": speech, "decisions": decisions}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out
