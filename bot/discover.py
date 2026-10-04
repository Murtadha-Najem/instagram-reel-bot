"""Discovery: the bot skims the Reels tab by itself and sends its owner what passes a scoring.

A session, a few times a day at random moments:
  1. skim 30 to 50 reels in the bot's own browser, reading only what the page loads by itself;
  2. one agent call scores every reel from its caption, numbers and cover picture;
  3. reels at like_score or more get a like, which is how the feed learns what to bring next;
  4. reels at send_score or more go through the full pipeline and research, are scored again, and are sent
     only if they still pass. A session may send many or none: there is no quota;
  5. everything seen is written to the log, whatever happened to it.
"""
import json
import random
import subprocess
import time
from datetime import datetime

import requests

from . import agent, browser, ig, watch
from .config import ROOT

AWAKE_HOURS = 16   # sessions_per_day is spread over a day of about this many hours


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def plan_next(cfg, first=False):
    """Pick the moment of the next session: no fixed times, only an average number a day."""
    cfg.discovery.mkdir(parents=True, exist_ok=True)
    if first:
        gap = random.uniform(5, 40) * 60
    else:
        gap = random.uniform(0.4, 1.6) * AWAKE_HOURS * 3600 / max(cfg.sessions_per_day, 0.1)
    (cfg.discovery / "state.json").write_text(json.dumps({"next_at": time.time() + gap}), encoding="utf-8")
    watch.log(cfg, f"discover: next session around {datetime.fromtimestamp(time.time() + gap):%a %H:%M}")


def due(cfg):
    if not cfg.discover:
        return False
    next_at = _read(cfg.discovery / "state.json", {}).get("next_at")
    if not next_at or time.time() - next_at > 3600:  # never planned, or the computer was off when it was due
        plan_next(cfg, first=True)
        return False
    return time.time() >= next_at


def seen_codes(cfg):
    try:
        with (cfg.discovery / "log.jsonl").open(encoding="utf-8") as fh:
            return {json.loads(ln)["code"] for ln in fh if ln.strip()}
    except OSError:
        return set()


def _agent(cfg, name, log_file, timeout, **values):
    """Run the agent on one of the discovery prompts."""
    rules = "\n".join(f"- {r}" for r in cfg.reply_rules) or "- (none)"
    prompt = (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8").format(
        owner=cfg.owner, python=agent.python_command(), root=ROOT.as_posix(), records=cfg.records.as_posix(),
        language=cfg.language, max_chars=cfg.max_chars, rules=rules,
        profile=cfg.discover_profile or "(no profile given: judge by general interest)",
        like_score=cfg.like_score, send_score=cfg.send_score, **values)
    prompt_file = log_file.with_suffix(".prompt.md")
    prompt_file.write_text(prompt, encoding="utf-8")
    try:
        with open(log_file, "w", encoding="utf-8") as fh:
            return subprocess.run(agent.command(cfg, prompt, prompt_file), cwd=ROOT, stdout=fh,
                                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  creationflags=agent.NO_WINDOW, timeout=timeout).returncode
    finally:
        prompt_file.unlink(missing_ok=True)


def _covers(cfg, items):
    folder = cfg.discovery / "covers"
    folder.mkdir(parents=True, exist_ok=True)
    for it in items:
        url = it.pop("cover_url", None)
        if not url:
            continue
        try:
            f = folder / f"{it['code']}.jpg"
            f.write_bytes(requests.get(url, timeout=30).content)
            it["cover"] = f.as_posix()
        except Exception:
            pass


def _score(cfg, stamp, items):
    """First scoring, from caption, numbers and cover. Returns {code: {score, reason, ...}}."""
    work = cfg.discovery / "sessions"
    work.mkdir(parents=True, exist_ok=True)
    cand, out = work / f"{stamp}.json", work / f"{stamp}.scores.json"
    cand.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    code = _agent(cfg, "discover", cfg.logs / f"discover_{stamp}.log", 1500,
                  candidates_file=cand.as_posix(), scores_file=out.as_posix())
    scores = {s["code"]: s for s in _read(out, []) if isinstance(s, dict) and s.get("code")}
    watch.log(cfg, f"discover: scored {len(scores)} of {len(items)} (agent exit {code})")
    return scores


def _deep(cfg, stamp, it):
    """Watch and research one promising reel; the agent sends it if it still passes. Returns its verdict."""
    work = cfg.discovery / "sessions"
    post = {"url": f"https://www.instagram.com/reel/{it['code']}/"}
    watch.analyse(cfg, post)
    if "bundle" not in post:
        return {"sent": False, "reason": f"would not open: {post.get('error', '')[:120]}"}
    brief = work / f"{stamp}_{it['code']}.json"
    verdict, reply = brief.with_suffix(".verdict.json"), brief.with_suffix(".reply.txt")
    brief.write_text(json.dumps({**it, **post}, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        _agent(cfg, "recommend", cfg.logs / f"discover_{stamp}_{it['code']}.log", 1800,
               brief_file=brief.as_posix(), verdict_file=verdict.as_posix(), reply_file=reply.as_posix())
        return _read(verdict, {"sent": False, "reason": "the agent left no verdict"})
    finally:
        for f in (brief, verdict, reply):
            f.unlink(missing_ok=True)


def _rows(cfg):
    """The log as it stands: one row per reel, a later line for the same reel replacing the earlier one."""
    rows = {}
    try:
        with (cfg.discovery / "log.jsonl").open(encoding="utf-8") as fh:
            for ln in fh:
                if ln.strip():
                    r = json.loads(ln)
                    rows[r["code"]] = r
    except OSError:
        pass
    return list(rows.values())


def _log(cfg, rows):
    """Record rows now (a session can be cut short by the computer going to sleep), then redraw log.md."""
    with (cfg.discovery / "log.jsonl").open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    sessions = {}
    for r in _rows(cfg):
        sessions.setdefault(r.get("session", ""), []).append(r)
    cell = lambda s: " ".join(str(s or "").split()).replace("|", "/")
    out = ["# Reels seen by discovery\n\nEvery reel the bot skimmed, newest session first: the score of its idea "
           "and what was done.\n"]
    for stamp in sorted(sessions, reverse=True):
        rs = sessions[stamp]
        try:
            when = f"{datetime.strptime(stamp, '%Y%m%d_%H%M%S'):%Y-%m-%d %H:%M}"
        except ValueError:
            when = stamp
        liked = sum(bool(r.get("like")) or r["action"] in ("sent", "liked", "checked", "pending") for r in rs)
        out.append(f"\n## {when} ({len(rs)} seen, {liked} liked, {sum(r['action'] == 'sent' for r in rs)} sent)\n\n"
                   "| score | action | account | idea | why | link |\n|---|---|---|---|---|---|\n")
        for r in sorted(rs, key=lambda r: -(r.get("final_score") or r.get("score") or 0)):
            score = "" if r.get("score") is None else f"{float(r['score']):g}"
            if r.get("final_score") is not None:
                score += f" then {float(r['final_score']):g}"   # first look, then after watching and research
            out.append(f"| {score} | {r['action']} | @{r.get('account') or '?'} | "
                       f"{cell(r.get('idea') or r.get('caption'))[:90]} | "
                       f"{cell(r.get('final_reason') or r.get('reason'))[:160]} | "
                       f"[open](https://www.instagram.com/reel/{r['code']}/) |\n")
    (cfg.discovery / "log.md").write_text("".join(out), encoding="utf-8")


def _check(cfg, it):
    """The deep check of one reel waiting for it, recorded as soon as it is decided."""
    v = _deep(cfg, it.get("session", "resume"), it)
    it.update(action="sent" if v.get("sent") else "checked", final_score=v.get("score"), final_reason=v.get("reason"))
    _log(cfg, [it])


def session(cfg, total=None):
    """One discovery session, start to finish."""
    cfg.discovery.mkdir(parents=True, exist_ok=True)
    try:
        with ig.file_lock(cfg.discovery / "session.lock", stale=3 * 3600, wait=1):
            _session(cfg, total)
    except TimeoutError:
        watch.log(cfg, "discover: a session is already running")
    except browser.InboxError as e:
        watch.log(cfg, f"discover: stopped: {e}")
    except Exception as e:
        watch.log(cfg, f"discover: error {type(e).__name__}: {e}")
    finally:
        plan_next(cfg)


def _session(cfg, total):
    for it in [r for r in _rows(cfg) if r["action"] == "pending"]:   # left over from a session that was cut short
        watch.log(cfg, f"discover: finishing the check of {it['code']}")
        _check(cfg, it)
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    total = total or random.randint(*cfg.session_reels)
    watch.log(cfg, f"discover: session of {total} reels")
    items = browser.reels(cfg, total)["items"]
    before = seen_codes(cfg)
    rows, fresh = [], []
    for it in items:
        it["seen_at"], it["session"] = datetime.now().isoformat(timespec="seconds"), stamp
        if it["code"] in before:
            continue  # shown again by Instagram: already in the log
        rows.append(it)
        if not it.get("account"):
            it.update(action="skipped", reason="the page gave no details for this one")
        else:
            fresh.append(it)
    _covers(cfg, rows)
    scores = _score(cfg, stamp, fresh) if fresh else {}
    for it in fresh:
        it.update(scores.get(it["code"], {"score": 0, "reason": "not scored"}))
        try:
            it["score"] = float(it.get("score") or 0)
        except (TypeError, ValueError):
            it["score"] = 0
        it["action"] = "liked" if it["score"] >= cfg.like_score else "skipped"
    for it in fresh:
        if it["score"] >= cfg.send_score:
            it["action"] = "pending"   # waiting for its deep check; a later session finishes what this one could not
    _log(cfg, rows)
    to_like = [it for it in fresh if it["action"] != "skipped"]
    if to_like:
        likes = browser.reels(cfg, like_codes=[it["code"] for it in to_like])["likes"]
        for it in to_like:
            it["like"] = likes.get(it["code"])
        _log(cfg, to_like)
    for it in sorted((i for i in fresh if i["action"] == "pending"), key=lambda i: -i["score"]):
        _check(cfg, it)
    watch.log(cfg, f"discover: {len(items)} skimmed, {len(rows)} new, {len(to_like)} liked, "
                   f"{sum(r['action'] == 'sent' for r in rows)} sent")
