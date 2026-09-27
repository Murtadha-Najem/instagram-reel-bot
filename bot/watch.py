"""The check that runs every few minutes, and the worker that answers one turn.

The check is plain HTTP: no browser and no agent when nothing is new. New messages are split into turns
(a shared post with the texts about it, or loose texts). Each turn is claimed in the state file and handed
to its own worker process: the reel pipeline, then one agent session that replies to that turn only. Up to max_workers turns run at once, so a
quick photo is not stuck behind a long video.
"""
import json
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from . import agent, browser, ig
from .config import ROOT

NO_WINDOW = agent.NO_WINDOW
MAX_TRIES = 2       # agent runs per turn before giving up on it
STALE = 45 * 60     # a claim older than this belongs to a worker that died


def log(cfg, msg):
    f = cfg.logs / "watch.log"
    try:
        if f.stat().st_size > 1_000_000:
            f.replace(cfg.logs / "watch.old.log")
    except OSError:
        pass
    with f.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")


def thread(cfg):
    try:
        return ig.owner_thread(cfg)
    except ig.LoggedOut:  # the plain-HTTP cookies went stale: refresh them from the browser profile once
        log(cfg, "cookies refused, refreshing them from the browser profile")
        subprocess.run([sys.executable, str(ROOT / "botctl.py"), "cookies"], cwd=ROOT,
                       creationflags=NO_WINDOW, capture_output=True, timeout=900)
        return ig.owner_thread(cfg)


# ---------- the check ----------

def check(cfg, jitter=True, dry_run=False):
    if jitter:
        time.sleep(random.uniform(0, 60))  # do not hit Instagram on the same second every time
    try:
        t = thread(cfg)
    except ig.LoggedOut:
        log(cfg, "logged out: run `python botctl.py login`")
        return
    if t is None:
        return  # no conversation with the owner yet
    if not dry_run and ig.start_here(cfg, t):
        log(cfg, "first run: existing messages marked as handled; answering from now on")
        return
    started = []
    with ig.state(cfg) as s:
        now = time.time()
        for tid in [k for k, a in s["active"].items() if now - a["started"] > STALE]:
            log(cfg, f"turn {tid}: claim expired, releasing")
            del s["active"][tid]
        free = cfg.max_workers - len(s["active"])
        turns = ig.group(ig.pending(t, s, include_active=False))
        if dry_run:
            for turn in turns:
                print(json.dumps([{k: v for k, v in i.items() if k != "timestamp"} for i in turn], ensure_ascii=False))
            raise SystemExit(0)  # leaves the state untouched
        for turn in turns:
            tid = turn[0]["item_id"]
            if s["tries"].get(tid, 0) >= MAX_TRIES or free <= 0:
                continue  # given up on it, or every worker busy (the next check picks it up)
            s["active"][tid] = {"items": [i["item_id"] for i in turn], "started": now}
            s["tries"][tid] = s["tries"].get(tid, 0) + 1
            free -= 1
            add_context(t, turn)
            (cfg.turns / f"{tid}.json").write_text(
                json.dumps({"thread_id": t["thread_id"], "items": turn}, ensure_ascii=False, indent=1), encoding="utf-8")
            started.append((tid, len(turn)))
    procs = []
    for tid, n in started:
        log(cfg, f"turn {tid}: {n} message(s), starting a worker")
        procs.append(subprocess.Popen([sys.executable, str(ROOT / "botctl.py"), "worker", tid], cwd=ROOT,
                                      creationflags=NO_WINDOW))
    for p in procs:  # the scheduler allows overlapping checks, so waiting here delays nothing
        p.wait()


def add_context(thread, turn):
    """When he used Instagram's reply on an earlier message, give the agent that message's text."""
    by_id = {it["item_id"]: it for it in thread.get("items", [])}
    for it in turn:
        target = by_id.get(it.get("replying_to"))
        if target:
            it["replying_to_text"] = ig.sent_text(target) or ig.describe(target).get("url", "")


# ---------- one worker ----------

def analyse(cfg, it):
    code = it["url"].rstrip("/").rsplit("/", 1)[-1]
    cached = cfg.cache / code / "bundle.md"
    if cached.exists():  # already analysed (a retry, or sent twice): reuse it
        it["bundle"] = str(cached)
        return
    t0 = time.time()
    p = subprocess.run([sys.executable, str(ROOT / "reel.py"), it["url"]], cwd=ROOT, env=cfg.pipeline_env(),
                       creationflags=NO_WINDOW, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=900)
    lines = [ln.strip() for ln in (p.stdout + "\n" + p.stderr).splitlines() if ln.strip()]
    err = next((ln for ln in lines if ln.startswith("REEL ERROR")), None)
    bundle = next((ln for ln in reversed(lines) if ln.endswith("bundle.md")), None)
    if bundle and not err:
        it["bundle"] = bundle
    else:
        it["error"] = err or (lines[-1] if lines else f"reel.py exit {p.returncode}")
    log(cfg, f"pipeline {it['url']} {time.time() - t0:.0f}s {'ok' if 'bundle' in it else it['error'][:120]}")


def save_image(cfg, tid, it):
    """A picture that is not a post (a story, an uploaded photo): keep it with the rest of the archive."""
    try:
        img = cfg.cache / f"dm_{it['item_id']}" / "image.jpg"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(requests.get(it["image_url"], timeout=60).content)
        it["image"] = str(img)
    except Exception as e:
        log(cfg, f"turn {tid}: image download failed ({type(e).__name__})")


def absorb_late_texts(cfg, tid, turn):
    """Pull in a question he typed after the post while the pipeline was running."""
    post = next((i for i in turn if i["type"] != "text"), None)
    if not post:
        return
    t = ig.owner_thread(cfg)
    with ig.state(cfg) as s:
        mine = s["active"].get(tid)
        if not mine:
            return
        for it in ig.pending(t, s, include_active=False):
            if it["type"] == "text" and 0 <= it["timestamp"] - post["timestamp"] <= ig.AFTER \
                    and it["item_id"] not in mine["items"]:
                turn.append(it)
                mine["items"].append(it["item_id"])
                log(cfg, f"turn {tid}: added a question sent after the post")
    add_context(t, turn)
    turn.sort(key=lambda x: x["timestamp"])


def worker(cfg, tid):
    f = cfg.turns / f"{tid}.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    turn = data["items"]
    t0 = time.time()
    handled = False
    try:
        for it in turn:
            if "url" in it:
                analyse(cfg, it)
            elif it.get("image_url"):
                save_image(cfg, tid, it)
        # The agent starts as soon as the analysis is done: a question that arrived meanwhile joins the turn,
        # one sent later is answered as a follow-up from the record.
        try:
            absorb_late_texts(cfg, tid, turn)
        except Exception as e:
            log(cfg, f"turn {tid}: late-text check failed ({type(e).__name__}), going on")
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        out = cfg.logs / f"agent_{datetime.now():%Y%m%d_%H%M%S}_{tid[-6:]}.log"
        code = agent.run(cfg, f, out)
        log(cfg, f"turn {tid}: agent exit {code}, {time.time() - t0:.0f}s in total, log {out.name}")
    except Exception as e:
        log(cfg, f"turn {tid}: worker error {type(e).__name__}: {e}")
    finally:
        with ig.state(cfg) as s:
            s["active"].pop(tid, None)
            done = set(s["done"])
            handled = (s["last_timestamp"] or 0) >= turn[-1]["timestamp"] or all(i["item_id"] in done for i in turn)
            if handled:
                s["tries"].pop(tid, None)
        if handled:
            f.unlink(missing_ok=True)


def loop(cfg):
    """For machines without a scheduler set up: check forever, every interval_minutes."""
    print(f"Checking every {cfg.interval_minutes} minutes for messages from @{cfg.owner}. Ctrl+C to stop.")
    while True:
        subprocess.Popen([sys.executable, str(ROOT / "botctl.py"), "check"], cwd=ROOT)
        time.sleep(cfg.interval_minutes * 60)
