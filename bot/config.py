"""Settings from config.toml, and every path the bot uses."""
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(os.environ.get("REEL_BOT_CONFIG") or ROOT / "config.toml")


def _user_dir():
    """Private per-user folder for the browser profile and cookies (never inside the repo).

    On Windows this is ~/.instagram-reel-bot, not AppData: a process started from a packaged (Store) app sees
    a private, redirected copy of AppData, so a login made from such a terminal would be invisible to the
    Task Scheduler. The home folder is the same for everyone."""
    if sys.platform == "win32":
        return Path.home() / ".instagram-reel-bot"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "instagram-reel-bot"


@dataclass
class Config:
    owner: str                      # the only account whose messages are answered
    browser: str = "msedge"         # Playwright channel: msedge, chrome, or chromium
    mode: str = "interval"          # "live" (a browser stays open and reacts at once) or "interval"
    interval_minutes: int = 5       # interval mode: how often to check
    live_fallback_minutes: int = 15  # live mode: a safety check this often, in case the page misses a message
    max_workers: int = 2
    agent: str = "claude"           # claude, codex, or custom
    agent_command: str = ""         # for custom: a command line, {prompt_file} is replaced
    agent_model: str = ""
    language: str = "English"
    max_chars: int = 350
    reply_rules: list = field(default_factory=list)
    gemini_keys: list = field(default_factory=list)
    agent_dirs: list = field(default_factory=list)  # extra folders the agent may read
    data_dir: Path = ROOT / "data"
    user_dir: Path = field(default_factory=_user_dir)
    cache_dir: Path = None          # default: data_dir/cache
    records_dir: Path = None        # default: data_dir/records
    chats_dir: Path = None          # default: data_dir/chats; conversations opened in Claude start here
    chat_dirs: list = field(default_factory=list)   # extra folders a conversation may read (e.g. his notes)
    discover: bool = False          # skim the Reels tab in sessions and send what passes the scoring
    discover_profile: str = ""      # what the owner cares about, in his own words
    sessions_per_day: float = 4     # about this many sessions a day, at random moments
    session_reels: tuple = (30, 50)  # reels skimmed in one session: a random number in this range
    like_score: int = 6             # a reel scoring this or more gets a like (it steers the feed)
    send_score: int = 8             # a reel scoring this or more is watched, researched, and sent if it holds
    discover_dir: Path = None       # default: data_dir/discover; the log of everything seen lives here

    # derived paths
    @property
    def profile(self): return self.user_dir / "browser-profile"
    @property
    def cookies(self): return self.user_dir / "cookies.txt"
    @property
    def cache(self): return self.cache_dir or self.data_dir / "cache"
    @property
    def records(self): return self.records_dir or self.data_dir / "records"
    @property
    def chats(self): return self.chats_dir or self.data_dir / "chats"
    @property
    def discovery(self): return self.discover_dir or self.data_dir / "discover"
    @property
    def state_file(self): return self.data_dir / "state.json"
    @property
    def turns(self): return self.data_dir / "turns"
    @property
    def logs(self): return self.data_dir / "logs"

    def pipeline_env(self):
        """Environment for reel.py: where to store things and which cookies and keys to use."""
        env = dict(os.environ, REEL_DATA=str(self.data_dir), REEL_CACHE=str(self.cache), REEL_RECORDS=str(self.records),
                   REEL_COOKIES=str(self.cookies), PYTHONIOENCODING="utf-8")
        if self.gemini_keys:
            env["GEMINI_API_KEY"] = ",".join(self.gemini_keys)
        return env


def load():
    if not CONFIG_FILE.exists():
        sys.exit(f"No config: copy config.example.toml to {CONFIG_FILE.name} and fill in your account.")
    raw = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    ig, sched, agent, reply = (raw.get(k, {}) for k in ("instagram", "schedule", "agent", "reply"))
    owner = (ig.get("owner") or "").lstrip("@").strip()
    if not owner or owner == "your_main_account":
        sys.exit(f"Set [instagram] owner in {CONFIG_FILE.name}: the account you will send posts from.")
    cfg = Config(
        owner=owner,
        browser=ig.get("browser", "msedge"),
        mode=sched.get("mode", "interval"),
        interval_minutes=int(sched.get("interval_minutes", 5)),
        live_fallback_minutes=int(sched.get("live_fallback_minutes", 15)),
        max_workers=int(sched.get("max_workers", 2)),
        agent=agent.get("kind", "claude"),
        agent_command=agent.get("command", ""),
        agent_model=agent.get("model", ""),
        language=reply.get("language", "English"),
        max_chars=int(reply.get("max_chars", 350)),
        reply_rules=list(reply.get("rules", [])),
        gemini_keys=list(raw.get("gemini", {}).get("api_keys", [])),
        agent_dirs=[Path(d).expanduser() for d in agent.get("extra_dirs", [])],
    )
    if cfg.mode not in ("live", "interval"):
        sys.exit(f'[schedule] mode must be "live" or "interval", not "{cfg.mode}".')
    paths = raw.get("paths", {})
    if paths.get("data_dir"):
        cfg.data_dir = Path(paths["data_dir"]).expanduser()
    if paths.get("user_dir"):
        cfg.user_dir = Path(paths["user_dir"]).expanduser()
    if paths.get("cache_dir"):
        cfg.cache_dir = Path(paths["cache_dir"]).expanduser()
    if paths.get("records_dir"):
        cfg.records_dir = Path(paths["records_dir"]).expanduser()
    chat = raw.get("chat", {})
    if chat.get("dir"):
        cfg.chats_dir = Path(chat["dir"]).expanduser()
    cfg.chat_dirs = [Path(d).expanduser() for d in chat.get("extra_dirs", [])]
    disc = raw.get("discover", {})
    cfg.discover = bool(disc.get("enabled", False))
    cfg.discover_profile = disc.get("profile", "").strip()
    cfg.sessions_per_day = float(disc.get("sessions_per_day", 4))
    cfg.session_reels = tuple(sorted(int(n) for n in disc.get("reels_per_session", [30, 50])))[:2]
    cfg.like_score = int(disc.get("like_score", 6))
    cfg.send_score = int(disc.get("send_score", 8))
    if disc.get("dir"):
        cfg.discover_dir = Path(disc["dir"]).expanduser()
    for d in (cfg.data_dir, cfg.user_dir, cfg.cache, cfg.records, cfg.turns, cfg.logs):
        d.mkdir(parents=True, exist_ok=True)
    return cfg
