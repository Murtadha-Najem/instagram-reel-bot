"""instagram-reel-bot: send posts to a helper Instagram account and get the answers back in the chat.

  python botctl.py login                 open a browser window and log in to the bot account (once)
  python botctl.py status                is the bot account logged in
  python botctl.py check [--now]         one check for new messages (what the scheduler runs)
  python botctl.py check --dry-run       show the turns that would be answered, change nothing
  python botctl.py run                   check forever in this terminal, every interval_minutes
  python botctl.py schedule install      run the check automatically (Windows Task Scheduler, macOS launchd,
                                         Linux systemd user timer)
  python botctl.py schedule remove       stop the automatic checks
  python botctl.py fetch                 list unhandled messages as JSON
  python botctl.py send "<text>" [--reply-to <item_id>]    send a reply (the agent uses this)
  python botctl.py done <item_id>...     mark messages as handled (the agent uses this)
  python botctl.py cookies               refresh the cookies file from the browser profile
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot import browser, config, ig, schedule, watch  # noqa: E402


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]
    cfg = config.load()

    if cmd == "login":
        print("LOGGED IN" if browser.login(cfg) else "LOGIN TIMED OUT")
    elif cmd == "status":
        browser.status(cfg)
    elif cmd == "cookies":
        browser.refresh_cookies(cfg)
        print("COOKIES WRITTEN")
    elif cmd == "check":
        watch.check(cfg, jitter="--now" not in args and "--dry-run" not in args, dry_run="--dry-run" in args)
    elif cmd == "worker" and len(args) == 1:
        watch.worker(cfg, args[0])
    elif cmd == "run":
        watch.loop(cfg)
    elif cmd == "schedule" and args[:1] in (["install"], ["remove"]):
        (schedule.install if args[0] == "install" else schedule.remove)(cfg)
    elif cmd == "fetch":
        t = ig.owner_thread(cfg)
        print(json.dumps(ig.pending(t, ig.read_state(cfg)), ensure_ascii=False, indent=1))
    elif cmd == "send" and len(args) in (1, 3) and (len(args) == 1 or args[1] == "--reply-to"):
        print(browser.send(cfg, args[0], args[2] if len(args) == 3 else None))
    elif cmd == "done" and args:
        try:
            t = ig.owner_thread(cfg)
        except ig.LoggedOut:
            browser.refresh_cookies(cfg)
            t = ig.owner_thread(cfg)
        try:
            ig.mark_done(cfg, t, args)
        except KeyError as e:
            sys.exit(f"INBOX ERROR: message(s) {e} not found in the conversation.")
        print("MARKED")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
