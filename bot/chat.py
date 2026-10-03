"""Opening a real Claude conversation about a post, instead of discussing it in the Instagram chat.

The owner writes something like "start a conversation about this reel". The answering agent prepares a brief,
and this module turns it into a saved Claude Code session (named after the post, with an opening message that
lays out what was found and where the discussion could go) and moves that session into the Claude desktop
app, where it waits in the sidebar for him to continue.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .agent import NO_WINDOW
from .config import ROOT


def open_chat(cfg, brief_path, title):
    if cfg.agent != "claude":
        sys.exit("CHAT ERROR: opening a conversation needs [agent] kind = \"claude\".")
    claude = shutil.which("claude") or "claude"
    cfg.chats.mkdir(parents=True, exist_ok=True)
    brief = Path(brief_path).read_text(encoding="utf-8")
    prompt = (ROOT / "prompts" / "chat.md").read_text(encoding="utf-8")
    prompt = prompt.replace("{owner}", cfg.owner).replace("{language}", cfg.language).replace("{brief}", brief)

    dirs = []
    for d in (cfg.records, cfg.cache, *cfg.agent_dirs, *cfg.chat_dirs):
        if str(d) not in dirs:
            dirs += [str(d)]
    cmd = [claude, "-p", prompt, "--name", title, "--output-format", "json",
           "--allowedTools", "Read", "Glob", "Grep", "WebSearch", "WebFetch",
           *[a for d in dirs for a in ("--add-dir", d)],
           "--strict-mcp-config"]  # the first turn needs no MCP servers; the desktop app brings its own later
    if cfg.agent_model:
        cmd += ["--model", cfg.agent_model]
    p = subprocess.run(cmd, cwd=cfg.chats, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=900)
    try:
        session_id = json.loads(p.stdout)["session_id"]
    except (ValueError, KeyError):
        sys.exit(f"CHAT ERROR: the session was not created ({(p.stderr or p.stdout).strip()[-300:]})")

    if _to_desktop(cfg, claude, session_id):
        return f"CHAT OPENED in the Claude desktop app: {title}"
    return (f"CHAT READY but not moved into the desktop app: {title}. "
            f"He can open it there with /resume, or run: claude --desktop --resume {session_id}")


def _to_desktop(cfg, claude, session_id):
    """`claude --desktop` refuses to run with redirected output, so it gets a console of its own (minimised)."""
    cmd = [claude, "--desktop", "--resume", session_id]
    try:
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 7  # SW_SHOWMINNOACTIVE
            p = subprocess.Popen(cmd, cwd=cfg.chats, startupinfo=si, creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            p = subprocess.Popen(cmd, cwd=cfg.chats)
        return p.wait(timeout=60) == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
