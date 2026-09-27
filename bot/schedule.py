"""Running `botctl.py check` every few minutes with the operating system's own scheduler.

Checks may overlap on purpose: a check waits for the workers it started, and the next check must still
run on time to pick up new messages.
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
    py, script, minutes = _python_no_console(), ROOT / "botctl.py", cfg.interval_minutes
    if sys.platform == "win32":
        ps = f"""
$a = New-ScheduledTaskAction -Execute '{py}' -Argument '"{script}" check' -WorkingDirectory '{ROOT}'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes {minutes})
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
     -MultipleInstances Parallel -ExecutionTimeLimit (New-TimeSpan -Minutes 50)
Register-ScheduledTask -TaskName '{NAME}' -Description 'Checks the bot account for new Instagram messages.' `
     -Action $a -Trigger $t -Settings $s -Force | Out-Null
"""
        _run(["powershell", "-NoProfile", "-Command", ps])
        print(f"Windows Task Scheduler task '{NAME}' runs a check every {minutes} minutes while you are logged in.")
    elif sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"com.{NAME}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.{NAME}</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{script}</string><string>check</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>StartInterval</key><integer>{minutes * 60}</integer>
  <key>StandardOutPath</key><string>{cfg.logs / 'launchd.log'}</string>
  <key>StandardErrorPath</key><string>{cfg.logs / 'launchd.log'}</string>
</dict></plist>
""", encoding="utf-8")
        _run(["launchctl", "unload", str(plist)], check=False)
        _run(["launchctl", "load", str(plist)])
        print(f"launchd agent {plist.name} runs a check every {minutes} minutes. Note: launchd does not start a "
              "new run while the previous one is still going, so use `python botctl.py run` if you need overlap.")
    else:
        unit = Path.home() / ".config" / "systemd" / "user"
        unit.mkdir(parents=True, exist_ok=True)
        (unit / f"{NAME}.service").write_text(
            # the check is started in the background so the next timer tick is not blocked by a running worker;
            # KillMode=process keeps that background check alive when the oneshot unit finishes
            f"[Unit]\nDescription=instagram-reel-bot check\n\n[Service]\nType=oneshot\nKillMode=process\n"
            f"WorkingDirectory={ROOT}\nExecStart=/bin/sh -c '\"{py}\" \"{script}\" check &'\n", encoding="utf-8")
        (unit / f"{NAME}.timer").write_text(
            f"[Unit]\nDescription=instagram-reel-bot every {minutes} minutes\n\n[Timer]\nOnBootSec=2min\n"
            f"OnUnitActiveSec={minutes}min\n\n[Install]\nWantedBy=timers.target\n", encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", f"{NAME}.timer"])
        print(f"systemd user timer {NAME}.timer runs a check every {minutes} minutes.")


def remove(cfg):
    if sys.platform == "win32":
        _run(["powershell", "-NoProfile", "-Command",
              f"Unregister-ScheduledTask -TaskName '{NAME}' -Confirm:$false"], check=False)
    elif sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"com.{NAME}.plist"
        _run(["launchctl", "unload", str(plist)], check=False)
        plist.unlink(missing_ok=True)
    else:
        _run(["systemctl", "--user", "disable", "--now", f"{NAME}.timer"], check=False)
        unit = Path.home() / ".config" / "systemd" / "user"
        for f in (f"{NAME}.service", f"{NAME}.timer"):
            (unit / f).unlink(missing_ok=True)
    print("Automatic checks removed.")


def _run(cmd, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit(f"{cmd[0]} failed: {(p.stderr or p.stdout).strip()[-400:]}")
    return p
