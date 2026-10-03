"""The bot account's real browser profile: logging in once, keeping the cookies file fresh, and sending replies.

Replies are typed into the message box of instagram.com like a person would, then confirmed through the API.
Only one browser can open the profile at a time: in live mode the always-open browser does the sending
(see live.py), otherwise each send opens the profile briefly and parallel workers queue on a lock.
"""
import json
import random
import sys
import time
import urllib.request

from . import ig

HOME = "https://www.instagram.com/direct/inbox/"
CHECKPOINT_PATHS = ("/challenge", "/checkpoint", "scraping_warning")


class InboxError(Exception):
    """Something the agent should stop on: logged out, blocked, or a reply that did not arrive."""


def _plain_user_agent(pw, cfg, channel):
    """The browser's user agent without the "Headless" marker, read once from a throwaway browser and cached.
    (Reading it from the profile itself meant opening the profile twice in a row, which could hang.)"""
    f = cfg.user_dir / "user_agent.txt"
    try:
        cached = f.read_text(encoding="utf-8").split("\n")
        if cached[0] == pw.chromium.executable_path + "|" + (channel or "") and cached[1]:
            return cached[1]
    except (OSError, IndexError):
        pass
    b = pw.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
    try:
        ua = b.new_page().evaluate("navigator.userAgent")
    finally:
        b.close()
    ua = ua.replace("HeadlessChrome", "Chrome").replace("Headless", "")
    f.write_text(pw.chromium.executable_path + "|" + (channel or "") + "\n" + ua, encoding="utf-8")
    return ua


def open_context(pw, cfg, headless=True):
    cfg.profile.mkdir(parents=True, exist_ok=True)
    channel = cfg.browser if cfg.browser != "chromium" else None
    kw = dict(headless=headless, locale="en-US", viewport={"width": 1280, "height": 860},
              args=["--disable-blink-features=AutomationControlled"], timeout=60000)
    if channel:
        kw["channel"] = channel
    if headless:  # headless browsers announce themselves in the user agent; present the ordinary one instead
        kw["user_agent"] = _plain_user_agent(pw, cfg, channel)
    return pw.chromium.launch_persistent_context(str(cfg.profile), **kw)


def logged_in(ctx):
    return any(c["name"] == "sessionid" and "instagram.com" in c["domain"] for c in ctx.cookies())


def write_cookies(cfg, ctx):
    """The profile's Instagram cookies as a Netscape file, for plain-HTTP reads and for yt-dlp."""
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in ctx.cookies():
        if "instagram.com" not in c["domain"]:
            continue
        lines.append("\t".join([c["domain"], "TRUE" if c["domain"].startswith(".") else "FALSE", c["path"],
                                "TRUE" if c["secure"] else "FALSE", str(int(max(c["expires"], 0))),
                                c["name"], c["value"]]))
    cfg.cookies.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_cookie_file(cfg, ctx):
    """Put the cookies file's session into the browser. Needed when the browser is started by a scheduler:
    on Windows, a headless Edge launched from Task Scheduler cannot decrypt its own saved cookies."""
    try:
        rows = [ln.split("\t") for ln in cfg.cookies.read_text(encoding="utf-8").splitlines()
                if ln and not ln.startswith("#")]
    except OSError:
        return False
    cookies = [{"name": r[5], "value": r[6], "domain": r[0], "path": r[2], "secure": r[3] == "TRUE",
                **({"expires": int(r[4])} if r[4] not in ("", "0") else {})}
               for r in rows if len(r) >= 7]
    if not any(c["name"] == "sessionid" for c in cookies):
        return False
    ctx.add_cookies(cookies)
    return logged_in(ctx)


def ready_page(cfg, ctx, url=HOME):
    """A page on instagram.com with the bot account logged in."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(2500, 4000))
    for _ in range(5):  # the cookie store loads a moment after start-up
        if logged_in(ctx):
            return page
        page.wait_for_timeout(2000)
    if not load_cookie_file(cfg, ctx):
        raise InboxError("INBOX ERROR: the browser profile is not logged in. Run: python botctl.py login")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(2500, 4000))
    return page


def session(cfg, fn, headless=True):
    """Run fn(page) inside the logged-in profile, one browser at a time."""
    from playwright.sync_api import sync_playwright
    with ig.file_lock(cfg.data_dir / "browser.lock", stale=300, wait=600), sync_playwright() as pw:
        ctx = open_context(pw, cfg, headless)
        try:
            return fn(ready_page(cfg, ctx))
        finally:
            ctx.close()


def api(page, path):
    """Call Instagram's API from inside the page, the way the web app does."""
    res = page.evaluate(
        """async ([path, appId]) => {
            const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
            const r = await fetch(path, {credentials: "include", headers: {
                "X-IG-App-ID": appId, "X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest"}});
            return {status: r.status, url: r.url, body: await r.text()};
        }""", [path, ig.APP_ID])
    if "checkpoint_required" in res["body"][:300] or any(k in res["url"] for k in CHECKPOINT_PATHS):
        raise InboxError("INBOX ERROR: Instagram wants the account owner to answer a warning or check. "
                         "Stop the bot and run: python botctl.py open")
    if res["status"] != 200 or "/accounts/login" in res["url"]:
        raise InboxError(f"INBOX ERROR: HTTP {res['status']} on {path} (logged out or blocked).")
    return json.loads(res["body"])


def page_thread(cfg, page):
    data = api(page, "/api/v1/direct_v2/inbox/?persistentBadging=true&limit=20&thread_message_limit=30")
    for t in data.get("inbox", {}).get("threads", []):
        users = t.get("users", [])
        if len(users) == 1 and users[0].get("username", "").lower() == cfg.owner.lower():
            t["owner_id"] = str(users[0].get("pk"))
            return t, data.get("viewer", {})
    return None, data.get("viewer", {})


def dismiss_popups(page):
    from playwright.sync_api import TimeoutError as PWTimeout
    for label in ("Not Now", "Not now", "Cancel"):
        btn = page.get_by_role("button", name=label)
        try:
            if btn.first.is_visible(timeout=1500):
                btn.first.click()
        except PWTimeout:
            pass


def quote_target(cfg, page, item):
    """Open Instagram's reply-to on the owner's message `item`. True if the quote box opened."""
    if item.get("item_type") == "text":
        needle, exact = item.get("text", "").strip(), True
        if len(needle) > 40:
            needle, exact = needle[:40], False
    else:  # a shared post: its bubble shows the post owner's username
        v = next((item.get(k) for k in ig.POST_KEYS if item.get(k)), None)
        v = v[0] if isinstance(v, list) else (v or {})
        media = v.get("clip") or v.get("media") or v
        needle, exact = ((media.get("user") or {}).get("username") if isinstance(media, dict) else None), True
    if not needle:
        return False
    button = f'[aria-label="Reply to message from {cfg.owner}"]'
    for el in reversed(page.get_by_text(needle, exact=exact).all()):
        # the nearest ancestor holding a reply button is this message's own row
        if not el.evaluate("""(e, sel) => {
                document.querySelectorAll('[data-quote-row]').forEach(n => n.removeAttribute('data-quote-row'));
                for (let p = e; p; p = p.parentElement)
                    if (p.querySelector(sel)) { p.setAttribute('data-quote-row', '1'); return true; }
                return false; }""", button):
            continue
        try:
            el.scroll_into_view_if_needed(timeout=5000)
            el.hover()
            page.wait_for_timeout(random.randint(500, 900))
            page.locator(f'[data-quote-row] div[role="button"]{button}').first.click(timeout=5000)
            page.wait_for_timeout(random.randint(500, 900))
            return True
        except Exception:
            return False
    return False


def send_on_page(cfg, page, text, reply_to=None):
    """Type one reply into the owner's thread on `page` and confirm it arrived. Returns a status line."""
    t, _ = page_thread(cfg, page)
    if not t:
        raise InboxError(f"INBOX ERROR: no conversation with @{cfg.owner} yet. Send the bot a message first.")
    page.goto(f"https://www.instagram.com/direct/t/{t['thread_id']}/", wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(3000, 5000))
    dismiss_popups(page)
    box = page.locator('div[role="textbox"][contenteditable="true"]').first
    box.wait_for(timeout=30000)
    quoted = False
    if reply_to:
        item = next((it for it in t.get("items", []) if it["item_id"] == reply_to), None)
        quoted = bool(item) and quote_target(cfg, page, item)
    box.click()
    page.wait_for_timeout(random.randint(400, 900))
    page.keyboard.insert_text(text)
    page.wait_for_timeout(random.randint(700, 1500))
    page.keyboard.press("Enter")
    for _ in range(10):  # confirm through the API that the message is now in the thread
        page.wait_for_timeout(2000)
        t2, viewer = page_thread(cfg, page)
        if any(str(it.get("user_id")) == str(viewer.get("pk")) and ig.sent_text(it) == text
               for it in t2.get("items", [])[:5]):
            write_cookies(cfg, page.context)  # keep the plain-HTTP cookies fresh while here
            return "SENT" + ("" if not reply_to else " (as a reply)" if quoted else " (quote not found, sent plain)")
    raise InboxError("SEND ERROR: typed and pressed Enter, but the message did not show up in the thread.")


def seen_on_page(cfg, page):
    """Open the owner's chat so Instagram shows him "Seen": he knows the bot has his message."""
    t, viewer = page_thread(cfg, page)
    if not t:
        return "no conversation yet"
    page.goto(f"https://www.instagram.com/direct/t/{t['thread_id']}/", wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(4000, 6000))
    dismiss_popups(page)
    page.bring_to_front()
    page.mouse.move(640, 430)  # a person's pointer over the conversation
    page.wait_for_timeout(2500)
    t2, _ = page_thread(cfg, page)
    seen = (t2.get("last_seen_at") or {}).get(str(viewer.get("pk")), {}).get("item_id")
    latest = (t2.get("items") or [{}])[0].get("item_id")
    return "SEEN" if seen and seen == latest else "OPENED (Instagram has not marked it seen yet)"


def _live(cfg, kind, payload=None):
    """Hand work to the always-open browser when live mode is running. None when it is not."""
    try:
        info = json.loads((cfg.data_dir / "live.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/{kind}", method="POST", data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json", "X-Token": info["token"]})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            res = json.loads(r.read())
    except OSError:
        return None  # the live browser is not answering: open the profile directly instead
    if not res.get("ok"):
        raise InboxError(res.get("status", "SEND ERROR"))
    return res["status"]


def send(cfg, text, reply_to=None):
    text = " ".join(text.split())  # one line: Enter sends
    return (_live(cfg, "send", {"text": text, "reply_to": reply_to})
            or session(cfg, lambda page: send_on_page(cfg, page, text, reply_to)))


def login(cfg):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.instagram.com/accounts/login/")
        print("Log in to the bot account in the browser window (waiting up to 15 minutes)...", flush=True)
        deadline = time.time() + 900
        while time.time() < deadline and not logged_in(ctx):
            time.sleep(3)
        ok = logged_in(ctx)
        if ok:
            page.wait_for_timeout(5000)  # let "save login info" and redirects settle
            write_cookies(cfg, ctx)
        ctx.close()
    return ok


def open_visible(cfg, url="https://www.instagram.com/"):
    """Show the bot's browser so its owner can deal with something only a person should (a warning, a check)."""
    from playwright.sync_api import sync_playwright
    with ig.file_lock(cfg.data_dir / "browser.lock", stale=3600, wait=60), sync_playwright() as pw:
        ctx = open_context(pw, cfg, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        print("The bot's browser is open. Close the window when you are done.", flush=True)
        try:  # closing the window closes the whole browser, so save the session while it is still open
            while ctx.pages:
                write_cookies(cfg, ctx)
                ctx.pages[0].wait_for_timeout(5000)
        except Exception:
            pass
        finally:
            try:
                ctx.close()
            except Exception:
                pass


# ---------- discovery: reading the Reels tab ----------

REELS = "https://www.instagram.com/reels/"
IN_VIEW = """(want) => {
  const out = [];
  for (const e of document.querySelectorAll('svg[aria-label]')) {
    const r = e.getBoundingClientRect();
    if (!r.width || r.top < 0 || r.bottom > innerHeight || r.left < 0 || r.right > innerWidth) continue;
    if (e.getAttribute('aria-label') === want) out.push([r.left + r.width / 2, r.top + r.height / 2]);
  }
  return out;
}"""


def _challenged(page):
    if "/accounts/login" in page.url:
        raise InboxError("INBOX ERROR: the browser profile is not logged in. Run: python botctl.py login")
    if any(k in page.url for k in CHECKPOINT_PATHS):
        raise InboxError("INBOX ERROR: Instagram wants the account owner to answer a warning or check. "
                         "Stop the bot and run: python botctl.py open")


def _media(o, known, ad=False):
    """Collect every reel in a piece of Instagram's own JSON, keyed by shortcode."""
    if isinstance(o, dict):
        user = o.get("user")
        if o.get("code") and isinstance(user, dict) and "like_count" in o and o["code"] not in known:
            cap = o.get("caption")
            covers = (o.get("image_versions2") or {}).get("candidates") or []
            known[o["code"]] = {
                "account": user.get("username"), "name": user.get("full_name"), "verified": user.get("is_verified"),
                "caption": (cap.get("text") if isinstance(cap, dict) else "") or "",
                "likes": o.get("like_count"), "comments": o.get("comment_count"),
                "plays": o.get("play_count") or o.get("ig_play_count") or o.get("view_count"),
                "seconds": o.get("video_duration"), "posted": o.get("taken_at"),
                "paid": bool(o.get("is_paid_partnership")), "ad": ad,
                "cover_url": covers[0].get("url") if covers else None,
            }
        for v in o.values():
            _media(v, known, ad)
    elif isinstance(o, list):
        for v in o:
            _media(v, known, ad)


def reels_start(page):
    """Open the Reels tab and start listening to what the page loads by itself. Returns the response box."""
    box = []
    page.on("response", lambda r: box.append(r) if "/graphql/" in r.url else None)
    page.goto(REELS, wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(4500, 7000))
    _challenged(page)
    return box


def _drain(box, known):
    while box:
        r = box.pop(0)
        try:
            body = r.text()
            if body.startswith("{"):
                _media(json.loads(body), known, ad="Ads" in (r.request.post_data or ""))
        except Exception:
            pass


def reels_scroll(page, box, known, n):
    """Move through n reels the way a person skims them. Makes no request of its own: the reels' details
    come from the responses the page asked for itself (the first ones are inside the page's HTML)."""
    if not known:
        try:
            for text in page.evaluate("""() => [...document.querySelectorAll('script[type="application/json"]')]
                                         .map(s => s.textContent).filter(t => t.includes('like_count'))"""):
                _media(json.loads(text), known)
        except Exception:
            pass
    out = []
    page.bring_to_front()
    for _ in range(n):
        page.wait_for_timeout(random.randint(2500, 6500))
        _challenged(page)
        _drain(box, known)
        code = page.url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if code != "reels" and not any(o["code"] == code for o in out):
            out.append({"code": code, **known.get(code, {})})
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(random.randint(700, 1500))
    return out


def reels_like(page, code):
    """Open one reel, like it and watch a little: the only positive signal the web Reels tab offers."""
    page.goto(f"{REELS}{code}/", wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(3500, 6000))
    _challenged(page)
    page.bring_to_front()
    hits = page.evaluate(IN_VIEW, "Like")
    if not hits:
        return "already liked" if page.evaluate(IN_VIEW, "Unlike") else "no like button"
    x, y = hits[0]
    page.mouse.move(x + random.randint(-3, 3), y + random.randint(-3, 3))
    page.wait_for_timeout(random.randint(300, 800))
    page.mouse.down()
    page.mouse.up()
    page.wait_for_timeout(random.randint(1200, 2000))
    ok = bool(page.evaluate(IN_VIEW, "Unlike"))
    page.wait_for_timeout(random.randint(4000, 9000))
    return "liked" if ok else "like did not register"


def reels_session(cfg, total, like_codes=()):
    """Without live mode: one visit to the profile that skims `total` reels and likes `like_codes`."""
    def run(page):
        out = {"items": [], "likes": {}}
        if total:
            box, known = reels_start(page), {}
            out["items"] = reels_scroll(page, box, known, total)
        for code in like_codes:
            out["likes"][code] = reels_like(page, code)
        write_cookies(cfg, page.context)
        return out
    return session(cfg, run)


def reels(cfg, total=0, like_codes=()):
    """Skim `total` reels and/or like some. In live mode the open browser does it in short steps, so replies
    and new messages are not held up for the whole session."""
    if _live(cfg, "reels", {"op": "start" if total else "stop"}) is None:
        return reels_session(cfg, total, like_codes)
    out = {"items": [], "likes": {}}
    try:
        if total:
            while len(out["items"]) < total:
                step = json.loads(_live(cfg, "reels", {"op": "scroll", "n": min(8, total - len(out["items"]))}))
                if not step:
                    break
                out["items"] += [i for i in step if not any(o["code"] == i["code"] for o in out["items"])]
                time.sleep(random.uniform(1, 4))
        for code in like_codes:
            out["likes"][code] = _live(cfg, "reels", {"op": "like", "code": code})
            time.sleep(random.uniform(2, 8))
    finally:
        try:
            _live(cfg, "reels", {"op": "stop"})
        except InboxError:
            pass
    return out


def mark_seen(cfg):
    return _live(cfg, "seen") or session(cfg, lambda page: seen_on_page(cfg, page))


def status(cfg):
    return _live(cfg, "status") or session(
        cfg, lambda page: f"LOGGED IN as @{page_thread(cfg, page)[1].get('username')}")


def refresh_cookies(cfg):
    return _live(cfg, "cookies") or session(cfg, lambda page: (write_cookies(cfg, page.context), "COOKIES WRITTEN")[1])
