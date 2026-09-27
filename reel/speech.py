"""Stage 4: transcribe speech (or on-screen song lyrics) with Gemini, only when on-screen text does not already carry it.

Keys and their rotation come from keys.py.
Gemini being unavailable (daily quota, network) is reported, never fatal: the bundle still carries frames and text.
"""
import json
import re
import time
from contextlib import nullcontext

from .common import ReelError, run_bytes

MODEL = "gemini-3.6-flash"
GEMINI_LIMIT = None        # a batch runner may set a shared semaphore here capping concurrent requests across workers
# Raised after a real post passed the old bar (1.2 words/s, 60% of windows) at 1.7 and 83% and still had gaps.
# Captions that really carry the speech run at about the speaking rate, 2 words a second or more.
CAPTION_WORDS_PER_SECOND = 2.0
CAPTION_SPREAD = 0.8
ATTEMPTS = 6

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "has_speech": {"type": "BOOLEAN"},
        "language": {"type": "STRING"},
        "segments": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"t": {"type": "STRING"}, "text": {"type": "STRING"}},
            "required": ["t", "text"]}},
    },
    "required": ["has_speech", "segments"],
}

# The dialect is named, never implied: a prompt listing only Iraqi words made Gemini label Egyptian speech Iraqi.
PROMPT_SPEECH = """Transcribe the speech in this audio track from an Instagram reel.

1. Verbatim, exactly as spoken. Keep the spoken dialect as it is pronounced, whatever it is (Iraqi, Egyptian, Gulf, Levantine, Maghrebi, Modern Standard, or any other). Identify the dialect from the audio itself; never assume one. Do not convert dialect to Modern Standard Arabic and do not correct grammar. English words spoken inside Arabic stay in English letters.
2. Spoken words only. Do not transcribe song lyrics or background music. If the only vocals are singing, set has_speech to false and return no segments.
3. Start a new segment at each sentence or speaker change. t is the start time as MM:SS.
4. If a stretch is unclear write [غير واضح] (or [unclear] in English speech). Never guess. Take particular care with negations, numbers and names.
5. language: the main spoken language and dialect as heard, for example "Egyptian Arabic", "Iraqi Arabic", "Gulf Arabic", "English".
Return JSON only."""

PROMPT_LYRICS = """This audio track from an Instagram reel carries a song whose words also appear on screen. Transcribe the sung lyrics.

1. Verbatim, as sung, in the original language and dialect. Do not translate, correct or complete them from memory: only what is audible.
2. Start a new segment at each sung line. t is the start time as MM:SS.
3. If a stretch is unclear write [غير واضح] (or [unclear]). Never guess.
4. has_speech is true when any words are sung or spoken. language: the language and dialect of the lyrics.
Return JSON only."""


class GeminiUnavailable(ReelError):
    """Speech needed transcribing but Gemini could not do it now."""


def captions_cover_speech(onscreen, windows, speech_seconds, duration):
    """True when burned-in text plausibly already carries the speech, so Gemini is not needed."""
    lines = [l for l in onscreen if l["band"] != "top"]
    speech_windows = [(w["start"], w["end"]) for w in windows if w["is_speech"]]
    if not speech_windows:
        speech_windows = [(s, min(s + 5.0, duration)) for s in range(0, int(duration) or 1, 5)]
        speech_seconds = duration
    if not lines or speech_seconds <= 0:
        return False, "no caption text"
    words = sum(len(l["text"].split()) for l in lines)
    rate = words / speech_seconds
    covered = sum(any(s - 1.0 <= l["t"] <= e for l in lines) for s, e in speech_windows)
    spread = covered / len(speech_windows)
    ok = rate >= CAPTION_WORDS_PER_SECOND and spread >= CAPTION_SPREAD
    return ok, f"{rate:.1f} caption words per speech second, present in {spread:.0%} of speech windows"


def _retry_delay(error, attempt):
    m = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+(?:\.\d+)?)s", str(error))
    return max(5.0 * (attempt + 1), float(m.group(1)) + 2 if m else 0.0)


def transcribe(video, lyrics=False):
    from google.genai import errors, types

    from . import keys
    audio = run_bytes(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video), "-vn",
                       "-ac", "1", "-ar", "16000", "-b:a", "32k", "-f", "mp3", "-"])
    rotator = keys.KeyRotator()
    contents = [PROMPT_LYRICS if lyrics else PROMPT_SPEECH, types.Part.from_bytes(data=audio, mime_type="audio/mp3")]
    config = {"temperature": 0.0, "max_output_tokens": 8192, "response_mime_type": "application/json",
              "response_schema": SCHEMA, "thinking_config": {"thinking_level": "low"}}
    last = None
    for attempt in range(ATTEMPTS):
        try:
            with GEMINI_LIMIT if GEMINI_LIMIT is not None else nullcontext():
                resp = rotator.client().models.generate_content(model=MODEL, contents=contents, config=config)
        except errors.APIError as e:
            last = e
            if e.code == 400 and "thinking" in str(e).lower() and "thinking_config" in config:
                config.pop("thinking_config")
                continue
            if e.code == 429 and "PerDay" in str(e):
                if rotator.rotate():
                    continue
                raise GeminiUnavailable(f"the daily free Gemini quota is used up on all {len(rotator)} keys; it resets tomorrow")
            if e.code in keys.ROTATE_ON and e.code != 429 and rotator.rotate():
                continue
            if e.code in (429, 500, 503):
                time.sleep(_retry_delay(e, attempt))
                continue
            raise GeminiUnavailable(f"Gemini refused the request ({e.code}): {str(e)[:200]}")
        except Exception as e:     # network: dropped connections, timeouts
            last = f"{type(e).__name__}: {str(e)[:150]}"
            time.sleep(5.0 * (attempt + 1))
            continue
        try:
            data = json.loads(resp.text or "{}")
        except ValueError:
            last = "unparseable response"
            continue
        usage = resp.usage_metadata
        return {
            "has_speech": bool(data.get("has_speech")) and bool(data.get("segments")),
            "language": data.get("language") or "",
            "segments": data.get("segments") or [],
            "tokens_in": getattr(usage, "prompt_token_count", 0) or 0,
            "tokens_out": getattr(usage, "candidates_token_count", 0) or 0,
        }
    raise GeminiUnavailable(f"Gemini failed after {ATTEMPTS} attempts: {str(last)[:200]}")
