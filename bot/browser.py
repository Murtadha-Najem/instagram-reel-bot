"""The bot account's real browser profile: logging in once, keeping the cookies file fresh, and sending replies.

Replies are typed into the message box of instagram.com like a person would, then confirmed through the API.
Only one browser can open the profile at a time, so parallel workers queue on a lock here.
"""
import json
import random
import sys
import time

from . import ig

HOME = "https://www.instagram.com/direct/inbox/"


def open_context(pw, cfg, headless=True):
    cfg.profile.mkdir(parents=True, exist_ok=True)
    kw = dict(headless=headless, locale="en-US", viewport={"width": 1280, "height": 860},
              args=["--disable-blink-features=AutomationControlled"])
    if cfg.browser != "chromium":
        kw["channel"] = cfg.browser
    ctx = pw.chromium.launch_persistent_context(str(cfg.profile), **kw)
    if headless:
        # headless browsers announce themselves in the user agent; present the ordinary one instead
        ua = ctx.pages[0].evaluate("navigator.userAgent") if ctx.pages else ""
        if "Headless" in ua:
            ctx.close()
            kw["user_agent"] = ua.replace("HeadlessChrome", "Chrome").replace("Headless", "")
            ctx = pw.chromium.launch_persistent_context(str(cfg.profile), **kw)
    return ctx


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


def session(cfg, fn, headless=True):
    """Run fn(page) on instagram.com inside the logged-in profile, one browser at a time."""
    from playwright.sync_api import sync_playwright
    with ig.file_lock(cfg.data_dir / "browser.lock", stale=300, wait=600), sync_playwright() as pw:
        ctx = open_context(pw, cfg, headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME, wait_until="domcontentloaded")
            page.wait_for_timeout(random.randint(2500, 4000))
            for _ in range(5):  # the cookie store loads a moment after start-up
                if logged_in(ctx):
                    break
                page.wait_for_timeout(2000)
            else:
                if not load_cookie_file(cfg, ctx):
                    sys.exit("INBOX ERROR: the browser profile is not logged in. Run: python botctl.py login")
                page.goto(HOME, wait_until="domcontentloaded")
                page.wait_for_timeout(random.randint(2500, 4000))
            return fn(page)
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
    if res["status"] != 200 or "/accounts/login" in res["url"]:
        sys.exit(f"INBOX ERROR: HTTP {res['status']} on {path} (logged out or blocked).")
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


def status(cfg):
    session(cfg, lambda page: print(f"LOGGED IN as @{page_thread(cfg, page)[1].get('username')}"))


def refresh_cookies(cfg):
    session(cfg, lambda page: write_cookies(cfg, page.context))


def send(cfg, text, reply_to=None):
    """Type a reply into the owner's thread and confirm it arrived. Returns a status line."""
    text = " ".join(text.split())  # one line: Enter sends

    def run(page):
        t, _ = page_thread(cfg, page)
        if not t:
            sys.exit(f"INBOX ERROR: no conversation with @{cfg.owner} yet. Send the bot a message first.")
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
        sys.exit("SEND ERROR: typed and pressed Enter, but the message did not show up in the thread.")
    return session(cfg, run)
