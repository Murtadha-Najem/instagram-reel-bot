"""Starting the bot automatically with the operating system's own scheduler, in either mode.

interval mode: `botctl.py check` every interval_minutes. Checks may overlap on purpose: a check waits for the
workers it started, and the next one must still run on time.
live mode: `botctl.py live` starts at log-in and is restarted if it stops.
"""
import subprocess
import sys
from pathlib import Path

from .config import ROOT

NAME = "instagram-reel-bot"


def _python_no_console():
    exe = Path(sys.executable)
    if sys.platform == "win32":
        w = exe.with_name("pythonw.exe")
        return w if w.exists() else exe
    return exe


def install(cfg):
    remove(cfg, quiet=True)  # switching modes replaces the old entry
    py, script, minutes, live = _python_no_console(), ROOT / "botctl.py", cfg.interval_minutes, cfg.mode == "live"
    cmd = "live" if live else "check"
    if sys.platform == "win32":
        if live:  # at log-in, plus a tick every 10 minutes that restarts it if it died (a running copy is left alone)
            trigger = ("$t = @((New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME), "
                       "(New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
                       "-RepetitionInterval (New-TimeSpan -Minutes 10)))")
            settings = "-MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0)"
        else:
            trigger = (f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
                       f"-RepetitionInterval (New-TimeSpan -Minutes {minutes})")
            settings = "-MultipleInstances Parallel -ExecutionTimeLimit (New-TimeSpan -Minutes 50)"
        ps = f"""
$a = New-ScheduledTaskAction -Execute '{py}' -Argument '"{script}" {cmd}' -WorkingDirectory '{ROOT}'
{trigger}
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable {settings}
Register-ScheduledTask -TaskName '{NAME}' -Description 'instagram-reel-bot ({cfg.mode} mode)' `
     -Action $a -Trigger $t -Settings $s -Force | Out-Null
"""
        _run(["powershell", "-NoProfile", "-Command", ps])
        where = f"Windows Task Scheduler task '{NAME}'"
    elif sys.platform == "darwin":
        plist = _plist()
        plist.parent.mkdir(parents=True, exist_ok=True)
        timing = ("<key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>" if live
                  else f"<key>StartInterval</key><integer>{minutes * 60}</integer>")
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.{NAME}</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{script}</string><string>{cmd}</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  {timing}
  <key>StandardOutPath</key><string>{cfg.logs / 'launchd.log'}</string>
  <key>StandardErrorPath</key><string>{cfg.logs / 'launchd.log'}</string>
</dict></plist>
""", encoding="utf-8")
        _run(["launchctl", "load", str(plist)])
        where = f"launchd agent {plist.name}"
    else:
        unit = _units()
        unit.mkdir(parents=True, exist_ok=True)
        if live:
            (unit / f"{NAME}.service").write_text(
                f"[Unit]\nDescription=instagram-reel-bot (live)\n\n[Service]\nWorkingDirectory={ROOT}\n"
                f"ExecStart=\"{py}\" \"{script}\" live\nRestart=always\nRestartSec=30\n\n"
                f"[Install]\nWantedBy=default.target\n", encoding="utf-8")
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", "--now", f"{NAME}.service"])
        else:
            # the check runs in the background so a busy worker never blocks the next tick;
            # KillMode=process keeps it alive when the oneshot unit finishes
            (unit / f"{NAME}.service").write_text(
                f"[Unit]\nDescription=instagram-reel-bot check\n\n[Service]\nType=oneshot\nKillMode=process\n"
                f"WorkingDirectory={ROOT}\nExecStart=/bin/sh -c '\"{py}\" \"{script}\" check &'\n", encoding="utf-8")
            (unit / f"{NAME}.timer").write_text(
                f"[Unit]\nDescription=instagram-reel-bot every {minutes} minutes\n\n[Timer]\nOnBootSec=2min\n"
                f"OnUnitActiveSec={minutes}min\n\n[Install]\nWantedBy=timers.target\n", encoding="utf-8")
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", "--now", f"{NAME}.timer"])
        where = f"systemd user unit {NAME}"
    if live:
        print(f"{where}: live mode starts when you log in and restarts if it stops "
              f"(fallback check every {minutes} minutes).")
    else:
        print(f"{where}: a check every {minutes} minutes.")


def remove(cfg, quiet=False):
    if sys.platform == "win32":
        _run(["powershell", "-NoProfile", "-Command",
              f"Unregister-ScheduledTask -TaskName '{NAME}' -Confirm:$false -ErrorAction SilentlyContinue"],
             check=False)
    elif sys.platform == "darwin":
        _run(["launchctl", "unload", str(_plist())], check=False)
        _plist().unlink(missing_ok=True)
    else:
        for u in (f"{NAME}.timer", f"{NAME}.service"):
            _run(["systemctl", "--user", "disable", "--now", u], check=False)
            (_units() / u).unlink(missing_ok=True)
    if not quiet:
        print("The bot no longer starts automatically.")


def _plist():
    return Path.home() / "Library" / "LaunchAgents" / f"com.{NAME}.plist"


def _units():
    return Path.home() / ".config" / "systemd" / "user"


def _run(cmd, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit(f"{cmd[0]} failed: {(p.stderr or p.stdout).strip()[-400:]}")
    return p
