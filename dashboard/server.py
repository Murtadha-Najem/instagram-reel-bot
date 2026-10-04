"""A local control page for the bot's discovery: watch what it saw and sent, and change how it works.

  python dashboard/server.py        then open http://localhost:8798

Everything is local: it reads the discovery log and the bot's log, rewrites the [discover] section of
config.toml, starts a session as a child process, and (on Windows) starts or stops the scheduled bot.
It listens on 127.0.0.1 only, and refuses a POST that does not come from its own page.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from bot import config, discover, ig, schedule  # noqa: E402

PORT = 8798
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ALLOWED_ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
_lock = threading.Lock()
_run = {"proc": None, "started": 0.0}
_task = {"at": 0.0, "data": None}


# ---------------------------------------------------------------- the bot itself
def _ps(script, timeout=40):
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "PowerShell failed").strip()[:300])
    return p.stdout.strip()


def task_state(fresh=False):
    """The scheduled task that keeps the bot running (Windows only)."""
    if sys.platform != "win32":
        return {"exists": False}
    if not fresh and _task["data"] and time.time() - _task["at"] < 20:
        return _task["data"]
    try:
        data = {"exists": True, "state": _ps(f"(Get-ScheduledTask -TaskName '{schedule.NAME}').State")}
    except Exception:
        data = {"exists": False}
    _task.update(at=time.time(), data=data)
    return data


def bot_alive(cfg):
    """Is the live browser answering? (interval mode has nothing to answer: the task state says it all)"""
    try:
        info = json.loads((cfg.data_dir / "live.json").read_text(encoding="utf-8"))
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=1):
            return True
    except (OSError, ValueError, KeyError):
        return False


def set_bot(on):
    if sys.platform != "win32":
        raise RuntimeError("تشغيل وإيقاف البوت من هنا يشتغل على ويندوز بس")
    name = schedule.NAME
    if on:
        _ps(f"Enable-ScheduledTask -TaskName '{name}' | Out-Null; Start-ScheduledTask -TaskName '{name}'")
    else:  # the task restarts the bot every 10 minutes, so stopping it means disabling the task too
        _ps(f"Disable-ScheduledTask -TaskName '{name}' | Out-Null; Stop-ScheduledTask -TaskName '{name}';"
            "Start-Sleep 2; Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'botctl\\.py' -or "
            "($_.Name -eq 'msedge.exe' -and $_.CommandLine -match 'instagram-reel-bot') } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    return task_state(fresh=True)


# ------------------------------------------------------------------- a session
def session_running(cfg):
    with _lock:
        if _run["proc"] is not None and _run["proc"].poll() is None:
            return True
    try:
        return ig.pid_alive(int((cfg.discovery / "session.lock").read_text() or 0))
    except (OSError, ValueError):
        return False


def start_session(cfg, reels):
    if reels is not None and not (isinstance(reels, int) and 5 <= reels <= 100):
        raise ValueError("عدد الريلز لازم يكون بين 5 و 100")
    if session_running(cfg):
        raise RuntimeError("أكو جلسة شغالة هسه، انتظرها تخلص")
    exe = Path(sys.executable)
    if exe.stem.lower() == "python" and exe.with_name("pythonw" + exe.suffix).exists():
        exe = exe.with_name("pythonw" + exe.suffix)
    cmd = [str(exe), str(ROOT / "botctl.py"), "discover"] + (["--reels", str(reels)] if reels else [])
    with _lock:
        _run.update(proc=subprocess.Popen(cmd, cwd=ROOT, creationflags=NO_WINDOW, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL), started=time.time())
    return {"running": True}


# ------------------------------------------------------------------------- log
def log_tail(cfg, lines=250, only_discover=False):
    path = cfg.logs / "watch.log"
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 200_000))
            rows = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    rows = [r for r in rows if re.match(r"\d{4}-\d\d-\d\d ", r)]
    if only_discover:
        rows = [r for r in rows if "discover:" in r]
    return rows[-lines:]


def last_problem(cfg):
    """The newest line that needs the owner: a logout, an Instagram check, or a failed session."""
    for line in reversed(log_tail(cfg, 400)):
        if "live: watching the inbox" in line or re.search(r"discover: \d+ skimmed", line):
            return None  # things worked after whatever came before
        if re.search(r"logged (the bot )?out|answer a warning or check|discover: (stopped|error)", line):
            return line
    return None


# ----------------------------------------------------------------------- state
def state():
    cfg = config.load()
    rows = discover._rows(cfg)
    sessions = {}
    for r in rows:
        s = sessions.setdefault(r.get("session", ""), {"id": r.get("session", ""), "seen": 0, "liked": 0, "sent": 0})
        s["seen"] += 1
        s["liked"] += bool(r.get("like")) or r["action"] in ("sent", "liked", "checked", "pending")
        s["sent"] += r["action"] == "sent"
    today = f"{datetime.now():%Y%m%d}"
    todays = [s for k, s in sessions.items() if k.startswith(today)]
    buckets = {"8-10": 0, "6-7": 0, "3-5": 0, "0-2": 0}
    for r in rows:
        v = r.get("score")
        if v is not None:
            buckets["8-10" if v >= 8 else "6-7" if v >= 6 else "3-5" if v >= 3 else "0-2"] += 1
    sent = [r for r in rows if r["action"] == "sent"]
    out_rows = [{
        "code": r["code"], "account": r.get("account"), "idea": r.get("idea") or (r.get("caption") or "")[:140],
        "score": r.get("score"), "final": r.get("final_score"), "action": r["action"],
        "why": r.get("final_reason") or r.get("reason") or "", "feedback": r.get("feedback"),
        "at": r.get("seen_at"),
    } for r in sorted(rows, key=lambda r: r.get("seen_at") or "", reverse=True)[:500]]
    return {
        "bot": {"mode": cfg.mode, "alive": bot_alive(cfg), "task": task_state(), "problem": last_problem(cfg)},
        "session": {"running": session_running(cfg),
                    "next_at": discover._read(cfg.discovery / "state.json", {}).get("next_at")},
        "settings": {"enabled": cfg.discover, "sessions_per_day": cfg.sessions_per_day,
                     "reels": list(cfg.session_reels), "like_score": cfg.like_score, "send_score": cfg.send_score,
                     "profile": cfg.discover_profile},
        "today": {"sessions": len(todays), "seen": sum(s["seen"] for s in todays),
                  "liked": sum(s["liked"] for s in todays), "sent": sum(s["sent"] for s in todays)},
        "totals": {"seen": len(rows), "sent": len(sent),
                   "liked_by_him": sum(r.get("feedback") in ("liked", "replied") for r in sent),
                   "rejected": sum(r.get("feedback") == "disliked" for r in sent), "buckets": buckets},
        "sessions": sorted(sessions.values(), key=lambda s: s["id"], reverse=True)[:12],
        "rows": out_rows,
    }


# -------------------------------------------------------------------- settings
def save_settings(data):
    cfg = config.load()
    v = {"enabled": cfg.discover, "sessions_per_day": cfg.sessions_per_day, "reels": list(cfg.session_reels),
         "like_score": cfg.like_score, "send_score": cfg.send_score, "profile": cfg.discover_profile}
    v.update({k: data[k] for k in v if k in data})
    if not isinstance(v["enabled"], bool):
        raise ValueError("قيمة التشغيل غير مفهومة")
    if not isinstance(v["sessions_per_day"], (int, float)) or not 0.5 <= v["sessions_per_day"] <= 12:
        raise ValueError("عدد الجلسات باليوم لازم يكون بين نص جلسة و 12")
    lo, hi = v["reels"] if isinstance(v["reels"], list) and len(v["reels"]) == 2 else (0, 0)
    if not all(isinstance(n, int) for n in (lo, hi)) or not 5 <= lo <= hi <= 100:
        raise ValueError("عدد الريلز بالجلسة لازم يكون بين 5 و 100، والأقل قبل الأكثر")
    if not all(isinstance(v[k], int) for k in ("like_score", "send_score")) \
            or not 1 <= v["like_score"] <= v["send_score"] <= 10:
        raise ValueError("الدرجات بين 1 و 10، ودرجة اللايك ما تزيد على درجة الإرسال")
    profile = (v["profile"] or "").strip()
    if len(profile) < 40:
        raise ValueError("وصف الاهتمامات قصير كلش، المقيّم ما راح يعرف شنو يختار")
    if "'''" in profile:
        raise ValueError("شيل الثلاث فواصل المتتالية من النص")
    block = "\n".join([
        "[discover]",
        "# written by the dashboard; the keys are explained in config.example.toml",
        f"enabled = {'true' if v['enabled'] else 'false'}",
        f"sessions_per_day = {v['sessions_per_day']:g}",
        f"reels_per_session = [{lo}, {hi}]",
        f"like_score = {v['like_score']}",
        f"send_score = {v['send_score']}",
        *([f'dir = "{cfg.discover_dir.as_posix()}"'] if cfg.discover_dir else []),
        f"profile = '''\n{profile}\n'''", "", ""])
    path = config.CONFIG_FILE
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(r"(?ms)^\[discover\]\n.*?(?=^\[|\Z)", lambda m: block, text, count=1)
    if not n:
        new = text.rstrip("\n") + "\n\n" + block
    backup = path.with_suffix(".toml.bak")
    backup.write_text(text, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    try:
        fresh = config.load()
    except BaseException:  # never leave the bot with a config it cannot read
        path.write_text(text, encoding="utf-8")
        raise ValueError("الإعدادات ما انقرت بعد الحفظ، رجعت النسخة القديمة")
    if fresh.sessions_per_day != cfg.sessions_per_day or (fresh.discover and not cfg.discover):
        discover.plan_next(fresh, first=not cfg.discover)
    return {"ok": True}


def set_feedback(code, value):
    if value not in ("liked", "disliked", None):
        raise ValueError("رأي غير معروف")
    cfg = config.load()
    row = next((r for r in discover._rows(cfg) if r["code"] == code), None)
    if not row:
        raise ValueError("هذا الريل مو بالسجل")
    row.update(feedback=value, feedback_emoji=None, feedback_from="dashboard")
    if value != "liked":
        row.pop("saved", None)
    discover._log(cfg, [row])
    return {"ok": True}


# ---------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        if body:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _trusted(self):
        """This page can change settings and stop the bot, so another site must not be able to POST to it:
        only our own origin, and only JSON bodies (which a plain cross-site form cannot send)."""
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return False
        return "application/json" in (self.headers.get("Content-Type") or "")

    def do_GET(self):
        path, _, query = self.path.partition("?")
        path = path.rstrip("/") or "/"
        try:
            if path == "/":
                return self._send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(state())
            if path == "/api/log":
                return self._json({"lines": log_tail(config.load(), 300, "discover=1" in query)})
            if path.startswith("/fonts/"):
                f = HERE / "fonts" / Path(path).name
                if f.suffix == ".woff2" and f.exists():
                    return self._send(200, f.read_bytes(), "font/woff2")
        except BaseException as e:
            return self._json({"error": str(e) or type(e).__name__}, 500)
        self.send_error(404)

    def do_POST(self):
        if not self._trusted():
            return self._json({"error": "forbidden"}, 403)
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            path = self.path.rstrip("/")
            if path == "/api/session":
                return self._json(start_session(config.load(), data.get("reels")))
            if path == "/api/settings":
                return self._json(save_settings(data))
            if path == "/api/feedback":
                return self._json(set_feedback(data.get("code"), data.get("feedback")))
            if path == "/api/bot":
                return self._json(set_bot(bool(data.get("on"))))
        except (ValueError, RuntimeError) as e:
            return self._json({"error": str(e)}, 400)
        except BaseException as e:
            return self._json({"error": str(e) or type(e).__name__}, 500)
        self.send_error(404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if sys.stdout:
        print(f"http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
