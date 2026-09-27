"""Live mode: one headless browser stays on the Instagram inbox and reacts the moment a message arrives.

The inbox page receives new messages in real time, like the app. A small script inside the page watches the
owner's chat row (no network requests of its own) and, when its last-message preview changes, the bot runs
a normal check straight away. A fallback check still runs every interval_minutes, in case the page misses
something. The same browser sends the replies: workers hand them over through a local HTTP endpoint that
only this machine can reach and that needs a random token, because the profile cannot be opened twice.
"""
import json
import queue
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import browser, ig
from .config import ROOT
from .watch import NO_WINDOW, log

WATCH_ROW = """
(() => {
  const NAME = %s;
  let last = null;
  setInterval(() => {
    // the chat row is the button around the text-only span holding the owner's display name
    const name = [...document.querySelectorAll('span')].find(s => !s.children.length && s.textContent.trim() === NAME);
    const row = name && name.closest('div[role="button"]');
    if (!row || !window.__reelbotPing) return;
    const leaves = [...row.querySelectorAll('span')].filter(s => !s.children.length)
                     .map(s => s.textContent.trim()).filter(Boolean);
    const preview = leaves[1] || '';   // [name, last message, "·", time]: the time is left out on purpose
    if (last === null) window.__reelbotPing('');          // first sight of the row
    else if (preview !== last) window.__reelbotPing(preview);
    last = preview;
  }, 1000);
})();
"""


class Endpoint(BaseHTTPRequestHandler):
    jobs = None   # queue of (kind, payload, reply_slot)
    token = ""

    def do_POST(self):
        if self.headers.get("X-Token") != self.token:
            self.send_error(403)
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        slot = {"done": threading.Event()}
        self.jobs.put((self.path.strip("/"), body, slot))
        slot["done"].wait(timeout=290)
        out = json.dumps(slot.get("result") or {"ok": False, "status": "SEND ERROR: the live browser did not answer"})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out.encode())

    def log_message(self, *a):
        pass


def _spawn_check(cfg, dry_run):
    args = [sys.executable, str(ROOT / "botctl.py"), "check", "--dry-run" if dry_run else "--now"]
    return subprocess.Popen(args, cwd=ROOT, creationflags=NO_WINDOW,
                            stdout=subprocess.DEVNULL if not dry_run else None)


def run(cfg, dry_run=False):
    """Live mode until stopped. Started by a scheduler there is no console, so every failure goes to the log."""
    try:
        _run(cfg, dry_run)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        log(cfg, f"live: stopped: {type(e).__name__}: {e}")
        raise


def _run(cfg, dry_run):
    from playwright.sync_api import sync_playwright

    jobs = queue.Queue()
    Endpoint.jobs, Endpoint.token = jobs, secrets.token_hex(16)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    info = cfg.data_dir / "live.json"
    pinged = threading.Event()

    with ig.file_lock(cfg.data_dir / "browser.lock", stale=10**9, wait=60), sync_playwright() as pw:
        ctx = browser.open_context(pw, cfg, headless=True)
        try:
            page = browser.ready_page(cfg, ctx)
            t, viewer = browser.page_thread(cfg, page)
            name = ((t or {}).get("users") or [{}])[0].get("full_name") or cfg.owner
            seen_row = threading.Event()
            page.expose_function("__reelbotPing", lambda preview: pinged.set() if preview else seen_row.set())
            ctx.add_init_script(WATCH_ROW % json.dumps(name))
            page.reload(wait_until="domcontentloaded")
            browser.write_cookies(cfg, ctx)
            info.write_text(json.dumps({"port": server.server_address[1], "token": Endpoint.token}), encoding="utf-8")
            log(cfg, f"live: watching the inbox of @{viewer.get('username')} for messages from @{cfg.owner}")
            print(f"Live: watching for messages from @{cfg.owner}. Ctrl+C to stop.", flush=True)

            # the check on a timer, in case the page misses something; the first one runs at once, for
            # anything that arrived while the bot was not running
            every = cfg.live_fallback_minutes * 60
            fallback = time.time() - every
            cookies_at = reload_at = time.time()
            last_check, pending_ping, proc = 0.0, False, None
            while True:
                page.wait_for_timeout(500)   # lets the page and its callbacks run
                now = time.time()
                if seen_row.is_set():
                    seen_row.clear()
                    log(cfg, "live: found the chat row in the page, reacting to new messages")
                if pinged.is_set():
                    pinged.clear()
                    pending_ping = True
                running = proc is not None and proc.poll() is None
                if pending_ping and not running and now - last_check > 5:
                    log(cfg, "live: new activity in the chat, checking")
                    proc, last_check, pending_ping, fallback = _spawn_check(cfg, dry_run), now, False, now
                elif now - fallback > every and not running:
                    proc, last_check, fallback = _spawn_check(cfg, dry_run), now, now
                while not jobs.empty():
                    kind, body, slot = jobs.get()
                    slot["result"] = _job(cfg, ctx, page, kind, body)
                    slot["done"].set()
                if now - cookies_at > 600:   # keep the plain-HTTP cookies fresh
                    browser.write_cookies(cfg, ctx)
                    cookies_at = now
                if now - reload_at > 2 * 3600:  # a long-lived page drifts; start it fresh now and then
                    page.goto(browser.HOME, wait_until="domcontentloaded")
                    reload_at = now
                if "/accounts/login" in page.url:
                    log(cfg, "live: Instagram logged the bot out; run `python botctl.py login`")
                    raise browser.InboxError("logged out")
                if any(k in page.url for k in browser.CHECKPOINT_PATHS):
                    log(cfg, "live: Instagram wants the account owner to answer a warning or check; "
                             "stop the bot and run `python botctl.py open`")
                    raise browser.InboxError("checkpoint")
        finally:
            info.unlink(missing_ok=True)
            server.shutdown()
            ctx.close()


def _job(cfg, ctx, page, kind, body):
    """Work handed over by other processes, done in this browser. Reads use the open page itself, so a check
    in live mode is just the web app asking its own API; anything that navigates gets a second tab."""
    if kind == "thread":
        try:
            t, viewer = browser.page_thread(cfg, page)
            return {"ok": True, "status": json.dumps({"thread": t, "viewer": viewer})}
        except browser.InboxError as e:
            return {"ok": False, "status": str(e)}
    tab = ctx.new_page()
    try:
        tab.goto(browser.HOME, wait_until="domcontentloaded")  # API calls need the instagram.com origin
        tab.wait_for_timeout(1500)
        if kind == "send":
            return {"ok": True, "status": browser.send_on_page(cfg, tab, body["text"], body.get("reply_to"))}
        if kind == "seen":
            return {"ok": True, "status": browser.seen_on_page(cfg, tab)}
        if kind == "cookies":
            browser.write_cookies(cfg, ctx)
            return {"ok": True, "status": "COOKIES WRITTEN"}
        if kind == "status":
            return {"ok": True, "status": f"LOGGED IN as @{browser.page_thread(cfg, tab)[1].get('username')} (live)"}
        return {"ok": False, "status": f"unknown request {kind}"}
    except browser.InboxError as e:
        return {"ok": False, "status": str(e)}
    except Exception as e:
        return {"ok": False, "status": f"SEND ERROR: {type(e).__name__}: {e}"}
    finally:
        tab.close()
