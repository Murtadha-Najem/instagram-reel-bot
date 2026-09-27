"""The whole pipeline for one link, with measurements. Used by reel.py and the bot."""
import time

from . import audio, bundle, download, speech, video
from .common import probe

SKIP_SPEECH = ("none", "silence", "music", "other sound")


def _photo_post(meta, max_frames, decisions):
    v = video.analyse_images(meta["images"], meta["dir"], max_frames, decisions)
    a = {"kind": "none (photo post)", "classified": False, "windows": [], "speech_seconds": 0.0, "song": None}
    music = meta.get("music")
    if music and music["type"] == "licensed song":
        a["song"] = {**music, "source": "Instagram music tag", "confirmed": True}
        decisions.append("song: from Instagram's music tag")
    return v, a, {"source": "none", "segments": []}


def _gemini(meta, lyrics, why, decisions, stats):
    t = time.time()
    try:
        result = speech.transcribe(meta["video"], lyrics=lyrics)
    except speech.GeminiUnavailable as e:
        stats["speech_s"] = round(time.time() - t, 1)
        stats["gemini_error"] = str(e)
        decisions.append(f"speech: needed ({why}) but NOT transcribed: {e}")
        return {"source": f"NOT transcribed, {e}", "segments": []}
    stats["speech_s"] = round(time.time() - t, 1)
    stats["gemini_in"], stats["gemini_out"] = result["tokens_in"], result["tokens_out"]
    decisions.append(f"speech: Gemini called ({why}; {result['tokens_in']} in / {result['tokens_out']} out tokens)")
    if not result["has_speech"]:
        source = "Gemini heard no words"
    else:
        source = f"Gemini {speech.MODEL}, sung lyrics" if lyrics else f"Gemini {speech.MODEL}"
    return {"source": source, "language": result["language"], "segments": result["segments"]}


def _video_post(meta, max_frames, force_transcribe, dense, decisions, stats):
    info = probe(meta["video"])
    duration = info["duration"]
    t = time.time()
    v = video.analyse(meta["video"], duration, meta["dir"], max_frames, decisions, merge_shots=not dense)
    stats["video_s"] = round(time.time() - t, 1)
    t = time.time()
    a = audio.analyse(meta["video"], duration, info["has_audio"], meta["music"], decisions)
    stats["audio_s"] = round(time.time() - t, 1)
    decisions.append(f"timing: video {stats['video_s']:.0f}s, audio {stats['audio_s']:.0f}s")

    # A song whose words are written on screen: its lyrics are content, so take them from the audio. Singing alone
    # (background music) still skips Gemini. The on-screen side can be either detected text changes or a strip
    # that changes constantly, which is how animated lyric videos register.
    singing = any(w["singing"] >= audio.SINGING_MIN for w in a["windows"])
    timeline = v.get("timeline") or {}
    words_on_screen = bool(v["text_changes"] or timeline.get("busy"))
    if a["kind"] == "music" and singing and words_on_screen:
        return duration, v, a, _gemini(meta, True, "singing with changing text on screen", decisions, stats)

    if a["kind"] in SKIP_SPEECH and not force_transcribe:
        decisions.append(f"speech: skipped, audio is {a['kind']}")
        return duration, v, a, {"source": "none", "segments": []}
    covered, why = speech.captions_cover_speech(v["onscreen"], a["windows"], a["speech_seconds"], duration)
    if covered and not force_transcribe:
        decisions.append(f"speech: Gemini skipped, captions carry it ({why})")
        return duration, v, a, {"source": "on-screen captions (see On-screen text)", "segments": []}
    return duration, v, a, _gemini(meta, False, why, decisions, stats)


def process(url, force_transcribe=False, refresh=False, max_frames=8, dense=False):
    """Download, analyse and bundle one link. Returns the bundle path and what each stage cost."""
    started = time.time()
    decisions, stats = [], {}
    t = time.time()
    meta = download.fetch(url, refresh=refresh)
    stats["fetch_s"] = round(time.time() - t, 1)
    stats["cached"] = meta["cached"]
    decisions.append("download: from cache" if meta["cached"] else "download: fresh")

    if meta["video"] is None:
        duration = 0.0
        v, a, s = _photo_post(meta, max_frames, decisions)
        stats["format"] = "photo"
    else:
        duration, v, a, s = _video_post(meta, max_frames, force_transcribe, dense, decisions, stats)
        stats["format"] = "video"

    path = bundle.write(meta, duration, v, a, s, decisions)
    stats.update(duration_s=round(duration, 1), frames_sent=len(v["frames"]), shots=v.get("shots"),
                 audio_kind=a["kind"], speech_source=s["source"], total_s=round(time.time() - started, 1))
    return {"shortcode": meta["shortcode"], "bundle": str(path), **stats}
