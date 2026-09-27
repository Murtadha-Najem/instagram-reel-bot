"""Stage 3: what kind of sound is this, and which song. Everything here is local or free."""
import asyncio
import contextlib
import io
import re
import time
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

from .common import run, run_bytes

PANNS_DIR = Path.home() / "panns_data"
PANNS_CKPT = PANNS_DIR / "Cnn14_mAP=0.431.pth"
PANNS_LABELS = PANNS_DIR / "class_labels_indices.csv"
SR = 32000
WINDOW = 5.0
SILENCE_DB = -45.0
SPEECH_MIN = 0.30
MUSIC_MIN = 0.30
SINGING_MIN = 0.20
SPEECH_LABELS = ("Speech", "Male speech, man speaking", "Female speech, woman speaking",
                 "Child speech, kid speaking", "Conversation", "Narration, monologue")
SHAZAM_SEGMENT = 12        # shazamio returns nothing at 15 s or more
SHAZAM_LIMIT = None        # a batch runner may set a shared semaphore here: Shazam's rate limit is undocumented, so one at a time
_TAGGER = {}


def mean_volume(video):
    p = run(["ffmpeg", "-hide_banner", "-i", str(video), "-vn", "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume: (-?[0-9.]+) dB", p.stderr)
    return float(m.group(1)) if m else None


def _tagger():
    """PANNs CNN14, loaded once per process. Rebuilding it for every reel cost 15-20 s."""
    if "model" not in _TAGGER:
        from panns_inference import AudioTagging
        with contextlib.redirect_stdout(io.StringIO()):     # keep its "Checkpoint path / Using CPU" out of the output
            _TAGGER["model"] = AudioTagging(checkpoint_path=str(PANNS_CKPT), device="cpu")
    return _TAGGER["model"]


def classify(video, duration):
    """Per 5 s window: speech / singing / music scores from PANNs CNN14 (AudioSet tags). None if the model is absent."""
    if not (PANNS_CKPT.exists() and PANNS_LABELS.exists()):
        return None
    import numpy as np
    from panns_inference import labels

    pcm = run_bytes(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video), "-vn",
                     "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"])
    wav = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    step = int(SR * WINDOW)
    chunks, spans = [], []
    for start in range(0, len(wav), step):
        chunk = wav[start:start + step]
        if len(chunk) < SR * 1.5 and chunks:
            break
        spans.append((start / SR, min((start + len(chunk)) / SR, duration)))
        chunks.append(np.pad(chunk, (0, step - len(chunk))))
    if not chunks:
        return None
    scores, _ = _tagger().inference(np.stack(chunks))
    idx = {name: i for i, name in enumerate(labels)}
    speech_idx = [idx[n] for n in SPEECH_LABELS if n in idx]

    windows = []
    for (s, e), row in zip(spans, scores):
        speech = float(max(row[i] for i in speech_idx))
        singing, music = float(row[idx["Singing"]]), float(row[idx["Music"]])
        windows.append({
            "start": s, "end": e,
            "speech": speech, "singing": singing, "music": music,
            # A sung vocal also scores as speech; only call it speech when it beats singing.
            "is_speech": speech >= SPEECH_MIN and speech >= singing,
            "is_music": music >= MUSIC_MIN or singing >= SINGING_MIN,
        })
    return windows


def summarise(windows):
    speech = [w for w in windows if w["is_speech"]]
    music_share = sum(w["is_music"] for w in windows) / len(windows)
    if speech:
        kind = "speech over music" if music_share >= 0.5 else "speech"
    else:
        kind = "music" if music_share > 0 else "other sound"
    return kind, sum(w["end"] - w["start"] for w in speech)


def identify_song(video, duration):
    from shazamio import Shazam

    if duration <= SHAZAM_SEGMENT + 2:
        offsets = [0.0]
    else:
        offsets = [0.0, max(duration / 2 - SHAZAM_SEGMENT / 2, 0.0), max(duration - SHAZAM_SEGMENT, 0.0)]

    async def recognise():
        shazam = Shazam(segment_duration_seconds=SHAZAM_SEGMENT)
        hits, errors = [], []
        for i, offset in enumerate(offsets):
            clip = run_bytes(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.2f}",
                              "-t", str(SHAZAM_SEGMENT), "-i", str(video), "-vn", "-ac", "1",
                              "-ar", "44100", "-f", "mp3", "-"])
            try:
                track = (await shazam.recognize(clip)).get("track")
            except Exception as e:     # unofficial API: report, never crash the reel
                errors.append(f"{type(e).__name__}: {str(e)[:120]}")
                track = None
            if track:
                hits.append((track.get("key"), track.get("title") or "", track.get("subtitle") or ""))
            if i < len(offsets) - 1:
                await asyncio.sleep(1.5)   # undocumented rate limit
        return hits, errors

    with SHAZAM_LIMIT if SHAZAM_LIMIT is not None else nullcontext():
        hits, errors = asyncio.run(recognise())
    if not hits:
        return {"error": errors[0]} if errors else None
    (key, title, artist), count = Counter(hits).most_common(1)[0]
    return {"title": title, "artist": artist, "matches": count, "windows": len(offsets),
            "confirmed": count >= 2 or len(offsets) == 1}


def analyse(video, duration, has_audio, instagram_music, decisions):
    out = {"kind": "none", "classified": False, "windows": [], "speech_seconds": 0.0, "song": None}
    if not has_audio:
        decisions.append("audio: no audio track")
        return out
    volume = mean_volume(video)
    if volume is not None and volume < SILENCE_DB:
        out["kind"] = "silence"
        decisions.append(f"audio: silent (mean {volume:.0f} dB)")
        return out

    t0 = time.time()
    windows = classify(video, duration)
    if windows is None:
        out["kind"] = "unknown"
        decisions.append("audio: classifier model not installed, kind unknown")
    else:
        out.update(classified=True, windows=windows)
        out["kind"], out["speech_seconds"] = summarise(windows)
        decisions.append(f"audio: {out['kind']} ({out['speech_seconds']:.0f}s speech, "
                         f"{len(windows)} windows, {time.time() - t0:.1f}s)")

    if instagram_music and instagram_music["type"] == "licensed song":
        out["song"] = {**instagram_music, "source": "Instagram music tag", "confirmed": True}
        decisions.append("song: from Instagram's music tag, Shazam skipped")
    elif out["kind"] in ("music", "speech over music", "unknown"):
        song = identify_song(video, duration)
        if song and "error" in song:
            decisions.append(f"song: Shazam unavailable ({song['error']})")
        elif song:
            out["song"] = {**song, "source": "Shazam"}
            decisions.append(f"song: Shazam matched {song['matches']} of {song['windows']} windows")
        else:
            decisions.append("song: Shazam found no match")
    return out
