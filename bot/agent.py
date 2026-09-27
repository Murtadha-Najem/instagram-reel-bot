"""Starting the coding agent that reads a turn and writes the reply: Claude Code, Codex, or your own command."""
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import ROOT

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def render_prompt(cfg, turn_file):
    rules = "\n".join(f"- {r}" for r in cfg.reply_rules) or "- (none)"
    text = (ROOT / "prompts" / "answer.md").read_text(encoding="utf-8")
    return text.format(
        owner=cfg.owner, python=Path(sys.executable).as_posix(), root=ROOT.as_posix(),
        records=cfg.records.as_posix(), language=cfg.language, max_chars=cfg.max_chars,
        rules=rules, turn_file=Path(turn_file).as_posix(),
    )


def _dirs(cfg):
    """Folders the agent may use besides the bot's own: its data, and any extra folders from the config."""
    out = []
    for d in (cfg.data_dir, cfg.cache, cfg.records, *cfg.agent_dirs):
        if str(d) not in out:
            out.append(str(d))
    return out


def command(cfg, prompt, prompt_file):
    py = Path(sys.executable).as_posix()
    add_dirs = [a for d in _dirs(cfg) for a in ("--add-dir", d)]
    if cfg.agent == "claude":
        exe = shutil.which("claude") or "claude"
        cmd = [exe, "-p", prompt,
               "--allowedTools", f"Bash({py}:*)", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch",
               *add_dirs,
               "--strict-mcp-config",           # no MCP servers: none are needed and loading them is slow
               "--disable-slash-commands",      # no skill listing either
               "--setting-sources", "project",  # leave the user's own hooks and settings out of these runs
               "--no-session-persistence"]
        if cfg.agent_model:
            cmd += ["--model", cfg.agent_model]
        return cmd
    if cfg.agent == "codex":
        exe = shutil.which("codex") or "codex"
        cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write",
               "-c", "sandbox_workspace_write.network_access=true", *add_dirs]
        if cfg.agent_model:
            cmd += ["--model", cfg.agent_model]
        return cmd + [prompt]
    if cfg.agent == "custom":
        if not cfg.agent_command:
            sys.exit("[agent] kind = custom needs a command, with {prompt_file} where the prompt file goes.")
        return [a.replace("{prompt_file}", str(prompt_file)) for a in shlex.split(cfg.agent_command, posix=True)]
    sys.exit(f"Unknown [agent] kind: {cfg.agent} (use claude, codex or custom)")


def run(cfg, turn_file, log_file, timeout=1800):
    prompt = render_prompt(cfg, turn_file)
    prompt_file = Path(turn_file).with_suffix(".prompt.md")
    prompt_file.write_text(prompt, encoding="utf-8")
    with open(log_file, "w", encoding="utf-8") as fh:
        p = subprocess.run(command(cfg, prompt, prompt_file), cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=timeout)
    prompt_file.unlink(missing_ok=True)
    return p.returncode
