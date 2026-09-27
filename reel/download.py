"""Stage 1: download the reel with yt-dlp and read everything Instagram says about it.

yt-dlp exposes only a handful of fields, but --write-pages saves Instagram's raw media response, which
carries about 500. The whole response is kept as media_raw.json; the fields worth reading are pulled into
metadata.json. Photo posts have no video for yt-dlp to return; their images come from the same response.
"""
import json
import re
import urllib.request
from datetime import datetime, timezone

from .common import CACHE_ROOT, COOKIES, PY, ReelError, run

SHORTCODE_RE = re.compile(r"instagram\.com/(?:[^/?#]+/)?(?:reels?|p|tv)/([A-Za-z0-9_-]+)")
HASHTAG_RE = re.compile(r"#([^\s#@.,!?،؛:\"']+)")
MENTION_RE = re.compile(r"@([A-Za-z0-9_.]+[A-Za-z0-9_])")
LOGIN_HINTS = ("login required", "rate-limit reached", "empty media response",
               "requested content is not available", "log in", "checkpoint_required")
NO_VIDEO_HINTS = ("no video formats found", "there is no video in this post")
MAX_IMAGES = 10
MEDIA_TYPES = {1: "image", 2: "video", 8: "carousel"}
ACCOUNT_TYPES = {1: "personal", 2: "business", 3: "creator"}


def shortcode(url):
    m = SHORTCODE_RE.search(url)
    if not m:
        raise ReelError(f"Not an Instagram reel or post link: {url}")
    return m.group(1)


def _yt_dlp(url, workdir):
    cmd = [PY, "-m", "yt_dlp", "--no-playlist", "--no-progress",
           # Width, not height: every portrait rendition is taller than 720, so height<=720 fell through to 1080x1920.
           "-f", "bv*[width<=720]+ba/b[width<=720]/bv*+ba/b",
           "--merge-output-format", "mp4", "-o", "video.%(ext)s",
           "--write-info-json", "--write-pages"]
    if COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    return run(cmd + [url], cwd=workdir)


def _video_result(url, code, workdir, cached):
    video, info = workdir / "video.mp4", workdir / "video.info.json"
    meta = _meta(url, code, info)
    details = _details(workdir)
    if details.get("posted_utc"):
        meta["date"] = details["posted_utc"][:10]
    return {"dir": workdir, "video": video, "images": [], "cached": cached,
            **meta, "music": _music(workdir), "details": details}


def fetch(url, refresh=False):
    code = shortcode(url)
    workdir = CACHE_ROOT / code
    workdir.mkdir(parents=True, exist_ok=True)
    video, info, post = workdir / "video.mp4", workdir / "video.info.json", workdir / "post.json"

    if not refresh:
        if video.exists() and info.exists():
            return _video_result(url, code, workdir, cached=True)
        if post.exists():
            saved = json.loads(post.read_text(encoding="utf-8"))
            return {"dir": workdir, "video": None, "cached": True, **saved,
                    "images": [workdir / "images" / name for name in saved["images"]]}

    for old in workdir.glob("*.dump"):
        old.unlink()
    for attempt in (1, 2):
        p = _yt_dlp(url, workdir)
        if p.returncode == 0 and video.exists():
            return _video_result(url, code, workdir, cached=False)
        err = p.stderr.lower()
        if any(h in err for h in NO_VIDEO_HINTS):
            media = _media(workdir)
            if media:
                return _photo_post(url, code, workdir, media)
            raise ReelError("Instagram returned this post without a video, and its images could not be read.")
        if any(h in err for h in LOGIN_HINTS):
            if not COOKIES.exists():
                raise ReelError(f"Instagram wants a login and there is no cookies file at {COOKIES}. "
                                "With the bot, run `python botctl.py login`.")
            if attempt == 2:
                raise ReelError(f"Instagram refused the cookies in {COOKIES}. They have probably expired: "
                                "with the bot, run `python botctl.py cookies` (or `login` again).")
        if attempt == 1:
            # Instagram changes often and a stale yt-dlp is the usual cause.
            run([PY, "-m", "pip", "install", "-U", "--quiet", "yt-dlp"])
            continue
        raise ReelError("yt-dlp failed: " + p.stderr.strip()[-600:])


def _date(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else ""


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else None


def _meta(url, code, info_path):
    info = json.loads(info_path.read_text(encoding="utf-8"))
    return {
        "url": url,
        "shortcode": code,
        "account": info.get("channel") or info.get("uploader_id") or "",
        "name": info.get("uploader") or "",
        "date": _date(info.get("timestamp")),
        "caption": (info.get("description") or "").strip(),
        "likes": info.get("like_count"),
        "views": info.get("view_count"),
        "comments": info.get("comment_count"),
    }


def _find_key(obj, key):
    if isinstance(obj, dict):
        if obj.get(key) is not None:
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = _find_key(child, key)
        if found is not None:
            return found
    return None


def _dumps(workdir):
    for dump in workdir.glob("*.dump"):
        try:
            yield json.loads(dump.read_bytes().decode("utf-8", "replace"))
        except ValueError:
            continue


def _music(workdir):
    """yt-dlp does not expose music fields, but --write-pages saved the raw API JSON that carries them."""
    for data in _dumps(workdir):
        asset = _find_key(data, "music_asset_info")
        if isinstance(asset, dict) and asset.get("title"):
            return {"type": "licensed song", "title": asset["title"], "artist": asset.get("display_artist") or ""}
        sound = _find_key(data, "original_sound_info")
        if isinstance(sound, dict):
            return {"type": "original audio",
                    "title": sound.get("original_audio_title") or "",
                    "artist": (sound.get("ig_artist") or {}).get("username") or ""}
    return None


def _media(workdir):
    for data in _dumps(workdir):
        if isinstance(data, dict) and data.get("items"):
            return data["items"][0]                                   # logged-in api/v1 media info
        node = _find_key(data, "xdt_shortcode_media") or _find_key(data, "shortcode_media")   # GraphQL
        if isinstance(node, dict):
            return node
    return None


def _usernames(items, key="user"):
    out = []
    for item in items or []:
        user = item.get(key) if isinstance(item, dict) and key else item
        if isinstance(user, dict) and user.get("username"):
            out.append(user["username"])
    return out


def _details(workdir):
    """Every field worth reading, from Instagram's own media response. The full response is kept alongside.

    Deliberately left out: other viewers (top likers, facepile), CDN URLs and rendering flags.
    """
    media = _media(workdir)
    if not isinstance(media, dict):
        return {}
    (workdir / "media_raw.json").write_text(json.dumps(media, ensure_ascii=False, indent=1), encoding="utf-8")
    g = media.get
    owner = g("user") or g("owner") or {}
    caption = g("caption") if isinstance(g("caption"), dict) else {}
    text = caption.get("text") or ""
    clips = g("clips_metadata") or {}
    music = ((clips.get("music_info") or {}).get("music_asset_info")
             or ((g("music_metadata") or {}).get("music_info") or {}).get("music_asset_info") or {})
    sound = clips.get("original_sound_info") or {}
    loc = g("location") or {}
    notes = (g("media_notes") or {}).get("items") or []
    duration = g("video_duration")
    details = {
        "media_id": g("pk"),
        "shortcode": g("code"),
        "product_type": g("product_type"),
        "media_type": MEDIA_TYPES.get(g("media_type"), g("media_type")),
        "owner": {k: v for k, v in {
            "username": owner.get("username"), "full_name": owner.get("full_name"), "id": owner.get("pk"),
            "verified": owner.get("is_verified"), "private": owner.get("is_private"),
            "account_type": ACCOUNT_TYPES.get(owner.get("account_type"), owner.get("account_type")),
        }.items() if v is not None},
        # Instagram rewrites taken_at on some posts (one read 14 May while its caption dates 12 May and a repost
        # 13 May), so the earliest timestamp on record is the real posting time.
        "posted_utc": _iso(min([t for t in (g("taken_at"), caption.get("created_at")) if t], default=None)),
        "caption_edited": g("caption_is_edited"),
        "hashtags": HASHTAG_RE.findall(text),
        "mentions": MENTION_RE.findall(text),
        "likes": g("like_count"),
        "comments": g("comment_count"),
        "plays": g("play_count") or g("ig_play_count"),
        "views": g("view_count"),
        "reposts": g("media_repost_count"),
        "reshares": g("reshare_count"),
        "counts_hidden_by_owner": g("like_and_view_counts_disabled") or None,
        "comments_disabled": g("disable_caption_and_comment") or g("comments_disabled") or None,
        "duration_s": round(duration, 1) if duration else None,
        "width": g("original_width"),
        "height": g("original_height"),
        "has_audio": g("has_audio"),
        "language": g("original_lang_for_translations"),
        "subtitles_locale": g("video_subtitles_locale"),
        "carousel_items": len(g("carousel_media") or []) or None,
        "carousel_types": [MEDIA_TYPES.get(i.get("media_type")) for i in g("carousel_media") or []] or None,
        "location": {k: v for k, v in {"name": loc.get("name"), "city": loc.get("city"), "address": loc.get("address"),
                                       "lat": loc.get("lat"), "lng": loc.get("lng")}.items() if v} or None,
        "audio_type": clips.get("audio_type"),
        "song": {k: v for k, v in {"title": music.get("title"), "artist": music.get("display_artist"),
                                   "explicit": music.get("is_explicit")}.items() if v is not None} if music.get("title") else None,
        "original_audio": {"title": sound.get("original_audio_title"),
                           "by": (sound.get("ig_artist") or {}).get("username")} if sound else None,
        "remixes": (clips.get("mashup_info") or {}).get("non_privacy_filtered_mashups_media_count"),
        "coauthors": _usernames(g("coauthor_producers"), key=None),
        "tagged_users": _usernames((g("usertags") or {}).get("in")),
        "paid_partnership": g("is_paid_partnership") or None,
        "sponsors": _usernames(g("sponsor_tags"), key="sponsor"),
        "accessibility_caption": g("accessibility_caption"),
        "ai_label_detection": (g("gen_ai_detection_method") or {}).get("detection_method"),
        "repost_notes": [{"by": (n.get("user") or {}).get("username"), "text": n.get("text"),
                          "at_utc": _iso(n.get("created_at"))} for n in notes] or None,
        "viewer_liked": g("has_liked"),
    }
    details = {k: v for k, v in details.items() if v not in (None, [], {}, "")}
    (workdir / "metadata.json").write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    return details


def _image_urls(media):
    if media.get("carousel_media"):
        items = media["carousel_media"]
    elif media.get("edge_sidecar_to_children"):
        items = [e["node"] for e in media["edge_sidecar_to_children"].get("edges", [])]
    else:
        items = [media]
    urls = []
    for item in items:
        candidates = (item.get("image_versions2") or {}).get("candidates") or []
        best = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0), default=None)
        url = best["url"] if best else item.get("display_url")
        if url:
            urls.append(url)
    return urls[:MAX_IMAGES]


def _photo_post(url, code, workdir, media):
    images_dir = workdir / "images"
    images_dir.mkdir(exist_ok=True)
    names = []
    for i, image_url in enumerate(_image_urls(media), 1):
        name = f"img_{i:02d}.jpg"
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            (images_dir / name).write_bytes(r.read())
        names.append(name)
    if not names:
        raise ReelError("Photo post found, but it carried no image URLs.")

    user = media.get("user") or media.get("owner") or {}
    caption = media.get("caption")
    caption = caption.get("text", "") if isinstance(caption, dict) else (_find_key(media.get("edge_media_to_caption") or {}, "text") or "")
    likes = media.get("like_count")
    if likes is None:
        likes = (media.get("edge_media_preview_like") or {}).get("count")
    saved = {
        "url": url, "shortcode": code,
        "account": user.get("username") or "", "name": user.get("full_name") or "",
        "date": _date(media.get("taken_at") or media.get("taken_at_timestamp")),
        "caption": (caption or "").strip(), "likes": likes, "views": None,
        "comments": media.get("comment_count"),
        "music": _music(workdir), "details": _details(workdir), "images": names,
    }
    (workdir / "post.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"dir": workdir, "video": None, "cached": False, **saved, "images": [images_dir / n for n in names]}
