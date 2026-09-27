"""Gemini API keys for speech transcription.

Keys come from the GEMINI_API_KEY environment variable (several may be separated by commas) and from
~/.config/reel/gemini_keys.txt (one key per line, # for comments). With several keys, a key that runs out of its daily
quota hands over to the next.
"""
import os
import random
from pathlib import Path

KEYFILE = Path.home() / ".config" / "reel" / "gemini_keys.txt"
ROTATE_ON = (400, 401, 403, 429)
_ORDER = []


def load_keys():
    if _ORDER:
        return list(_ORDER)
    keys = [k.strip() for k in os.environ.get("GEMINI_API_KEY", "").split(",") if k.strip()]
    if KEYFILE.exists():
        extra = [l.strip() for l in KEYFILE.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
        random.shuffle(extra)
        keys += [k for k in extra if k not in keys]
    _ORDER[:] = keys
    return list(keys)


class KeyRotator:
    """Hands out clients, moving to the next key when the current one is out of quota."""

    def __init__(self, start=0):
        self._keys = load_keys()
        if not self._keys:
            raise RuntimeError(f"no Gemini API key: set GEMINI_API_KEY or put one key per line in {KEYFILE}")
        self._i = start % len(self._keys)
        self._tried = 0
        self._clients = {}

    def client(self):
        from google import genai
        key = self._keys[self._i]
        if key not in self._clients:
            self._clients[key] = genai.Client(api_key=key)
        return self._clients[key]

    def rotate(self):
        if self._tried + 1 >= len(self._keys):
            return False
        self._tried += 1
        self._i = (self._i + 1) % len(self._keys)
        return True

    def __len__(self):
        return len(self._keys)
