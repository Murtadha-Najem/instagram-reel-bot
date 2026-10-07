"""The owner's messages as the bot sees them, and the handled-message state.

The conversation itself is read by the browser (browser.read_thread), from what Instagram's own page loads:
in live mode by the open browser, otherwise by opening the profile for the read.
"""
import json
import os
import re
import time
import urllib.request
from contextlib import contextmanager


class LoggedOut(Exception):
    pass


# ---------- reading ----------

def _via_live(cfg):
    """In live mode, ask the open browser: the read then comes from the web app itself, with the same
    browser, cookies and user agent as everything else, instead of from a second, different-looking client."""
    try:
        info = json.loads((cfg.data_dir / "live.json").read_text(encoding="utf-8"))
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/thread", method="POST", data=b"{}",
                                     headers={"Content-Type": "application/json", "X-Token": info["token"]})
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.loads(r.read())
    except (OSError, ValueError, KeyError):
        return None  # live mode is not running
    if not res.get("ok"):
        raise LoggedOut(res.get("status", "live browser could not read the inbox"))
    t = json.loads(res["status"])["thread"]
    return t or {}


def owner_thread(cfg, messages=50):
    """The one-to-one thread with the owner (None if there is none yet). Raises LoggedOut when it cannot be read."""
    t = _via_live(cfg)
    if t is not None:
        return t or None
    # Without live mode the profile is opened for the read. The plain-HTTP read this used to be relied on
    # /api/v1/direct_v2/inbox/, which Instagram retired; the page's own data is the only source left.
    from . import browser
    try:
        return browser.session(cfg, lambda page: browser.read_thread(cfg, page)[0])
    except browser.InboxError as e:
        raise LoggedOut(str(e))


INSTAGRAM_POST = re.compile(r"https://(www\.)?instagram\.com/(p|reel|reels|tv)/[\w-]+")
CDN = ("fbcdn.net", "cdninstagram.com")
TEXT_KEYS = {"text", "title", "caption", "subtitle", "title_text", "subtitle_text", "header_title_text",
             "preview_text", "description", "link_title", "link_summary"}
POST_KEYS = ("clip", "media_share", "direct_media_share", "xma_clip", "xma_media_share", "felix_share")


def describe(it):
    """One message as the agent sees it: text, or a post link the pipeline can open, or a fallback."""
    typ = it.get("item_type")
    out = {"item_id": it["item_id"], "timestamp": int(it["timestamp"]), "type": typ}
    if typ == "text":
        out["text"] = it.get("text", "")
    replied = (it.get("replied_to_message") or {})
    if replied.get("item_id"):
        out["replying_to"] = replied["item_id"]
    for key in POST_KEYS:
        v = it.get(key)
        if not v:
            continue
        v = v[0] if isinstance(v, list) else v
        media = v.get("clip") or v.get("media") or v.get("video") or v
        code = media.get("code") if isinstance(media, dict) else None
        if code:
            kind = "reel" if key in ("clip", "xma_clip", "felix_share") else "p"
            out["url"] = f"https://www.instagram.com/{kind}/{code}/"
        elif isinstance(v, dict) and INSTAGRAM_POST.match(v.get("target_url") or ""):
            out["url"] = v["target_url"]
    if typ != "text" and "url" not in out:
        out.update(fallback(it))
    return out


def fallback(it):
    """For item types the pipeline cannot open (story, Threads post, link, uploaded photo):
    whatever text, links and picture the item carries."""
    texts, links, image = [], [], None

    def walk(o, key=""):
        nonlocal image
        if isinstance(o, dict):
            if image is None and isinstance(o.get("candidates"), list) and o["candidates"]:
                image = (o["candidates"][0] or {}).get("url")
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str) and o.strip():
            if o.startswith("http"):
                if any(c in o for c in CDN):
                    if image is None and key in ("preview_url", "image_url", "link_image_url", "thumbnail_url", "url"):
                        image = o
                elif o not in links:
                    links.append(o)
            elif key in TEXT_KEYS and o not in texts:
                texts.append(o)

    walk({k: v for k, v in it.items() if k not in ("item_id", "user_id", "timestamp", "client_context")})
    ig_post = next((u for u in links if INSTAGRAM_POST.match(u)), None)
    if ig_post:
        return {"url": ig_post}
    out = {"note": f"not a post the reel pipeline can open ({it.get('item_type')})"}
    if texts:
        out["texts"] = texts[:8]
    if links:
        out["links"] = links[:5]
    if image:
        out["image_url"] = image
    return out


def sent_text(item):
    """A message's text. Instagram stores a message it thinks holds a link ("Three.js", "2.78") as a
    `link` item with the text under link.text, so the top-level text alone misses it."""
    raw = item.get("text") or (item.get("link") or {}).get("text") or ""
    return " ".join(raw.split())


def owner_items(thread):
    if not thread:
        return []
    return sorted((describe(it) for it in thread.get("items", []) if str(it.get("user_id")) == thread["owner_id"]),
                  key=lambda x: x["timestamp"])


# ---------- state, shared by parallel workers ----------
# state.json: {"last_timestamp": every owner message at or before this is handled,
#              "done": ids handled after it, "active": {turn_id: {"items": [...], "started": t}},
#              "tries": {turn_id: n}}

def pid_alive(pid):
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@contextmanager
def file_lock(path, stale=120, wait=300):
    """Exclusive lock through an O_EXCL file. Taken over when its holder has died, or is older than `stale` s."""
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                holder = int(path.read_text() or 0)
                if not pid_alive(holder) or time.time() - path.stat().st_mtime > stale:
                    path.unlink(missing_ok=True)
                    continue
            except (OSError, ValueError):
                continue
            if time.time() > deadline:
                raise TimeoutError(f"lock {path.name} busy")
            time.sleep(0.5)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def read_state(cfg):
    try:
        s = json.loads(cfg.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    for k, v in (("last_timestamp", None), ("done", []), ("active", {}), ("tries", {})):
        s.setdefault(k, v)
    return s


@contextmanager
def state(cfg):
    """Read-modify-write the state under the lock."""
    with file_lock(cfg.data_dir / "state.lock", stale=30, wait=60):
        s = read_state(cfg)
        yield s
        cfg.state_file.write_text(json.dumps(s, indent=1), encoding="utf-8")


def pending(thread, s, include_active=True):
    """The owner's unhandled messages, oldest first (optionally leaving out those a worker has claimed).
    Before the first run has set a starting point nothing is pending, so old history is never answered."""
    if s["last_timestamp"] is None:
        return []
    claimed = set() if include_active else {i for a in s["active"].values() for i in a["items"]}
    skip = set(s["done"]) | claimed
    return [it for it in owner_items(thread) if it["timestamp"] > s["last_timestamp"] and it["item_id"] not in skip]


def start_here(cfg, thread):
    """First run: everything already in the thread counts as handled."""
    with state(cfg) as s:
        if s["last_timestamp"] is None:
            items = owner_items(thread)
            s["last_timestamp"] = items[-1]["timestamp"] if items else int(time.time() * 1e6)
            return True
    return False


def mark_done(cfg, thread, item_ids):
    known = {it["item_id"] for it in owner_items(thread)}
    missing = [i for i in item_ids if i not in known]
    if missing:
        raise KeyError(", ".join(missing))
    with state(cfg) as s:
        done = set(s["done"]) | set(item_ids)
        # move the watermark over every handled message at the start of the list, and forget those ids
        for it in owner_items(thread):
            if it["timestamp"] <= (s["last_timestamp"] or 0):
                continue
            if it["item_id"] not in done:
                break
            s["last_timestamp"] = it["timestamp"]
            done.discard(it["item_id"])
        s["done"] = sorted(done)


# ---------- turns ----------

AFTER = 180_000_000   # microseconds: a text sent up to 3 minutes after a post belongs to it
BEFORE = 10_000_000   # a text sent a few seconds before a post also belongs to it (delivery order is not guaranteed)


def group(items):
    """Split messages into turns. A text belongs to the latest post sent up to 3 minutes before it; a text sent
    before a post does not, except within a few seconds, because Instagram sometimes delivers the text just
    ahead of the post sent with it. Other texts are messages of their own (consecutive ones within 3 minutes
    are read together)."""
    posts = [i for i in items if i["type"] != "text"]
    turns = {p["item_id"]: [p] for p in posts}
    loose = []
    for t in (i for i in items if i["type"] == "text"):
        prev = [p for p in posts if 0 <= t["timestamp"] - p["timestamp"] <= AFTER]
        nxt = [p for p in posts if 0 < p["timestamp"] - t["timestamp"] <= BEFORE]
        target = prev[-1] if prev else (nxt[0] if nxt else None)
        if target:
            turns[target["item_id"]].append(t)
        elif loose and t["timestamp"] - loose[-1][-1]["timestamp"] <= AFTER:
            loose[-1].append(t)
        else:
            loose.append([t])
    out = [sorted(v, key=lambda x: x["timestamp"]) for v in list(turns.values()) + loose]
    return sorted(out, key=lambda turn: turn[0]["timestamp"])
