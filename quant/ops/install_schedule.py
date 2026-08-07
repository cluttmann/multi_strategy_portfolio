"""Installiert ALLE launchd-Jobs für Daten + Execution (Burn-in).

    python3 -m quant.ops.install_schedule --install    # plists schreiben + laden
    python3 -m quant.ops.install_schedule --uninstall  # alle entladen
    python3 -m quant.ops.install_schedule --list       # Status

Zeiten in Europe/Berlin (launchd nutzt lokale Mac-Zeit). Jeder Execution-Job
ruft den jeweiligen Executor; der Handelstag-Guard (guard.py) macht Feiertags-
/Wochenend-Läufe zu No-Ops, Wochentag-Filter (Weekday 1-5) spart die
Wochenenden ganz. Jobs schreiben Logs nach quant/_staging/.

RUNBOOK-Zeitplan (Berlin / ET):
  03:00  Daten-Ops (daily.sh)                              [täglich]
  14:30  XSR plan            (08:30 ET, vor US-Open)        [Mo-Fr]
  15:00  XSR execute (opg)   (09:00 ET)                     [Mo-Fr]
  15:15  ONX exit (opg)      (09:15 ET)                     [Mo-Fr]
  16:15  Reconcile XSR+ONX   (10:15 ET)                     [Mo-Fr]
  21:45  ONX decide          (15:45 ET)                     [Mo-Fr]
  21:50  ONX enter (cls)     (15:50 ET)                     [Mo-Fr]
"""

import argparse
import os
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PY = sys.executable
LA = os.path.expanduser("~/Library/LaunchAgents")
LOG = os.path.join(REPO, "quant", "_staging")

# (label-suffix, Stunde, Minute, Modul-Args, nur Mo-Fr)
JOBS = [
    ("dailyops", 3, 0, ["quant.ops._run_daily"], False),
    ("xsr-plan", 14, 30, ["quant.execution.xsr_live", "--plan"], True),
    ("xsr-execute", 15, 0, ["quant.execution.xsr_live", "--execute"], True),
    ("onx-exit", 15, 15, ["quant.execution.onx_live", "--exit"], True),
    ("reconcile", 16, 15, ["quant.ops._reconcile"], True),
    ("onx-decide", 21, 45, ["quant.execution.onx_live", "--decide"], True),
    ("onx-enter", 21, 50, ["quant.execution.onx_live", "--enter"], True),
    ("sleeve-health", 22, 30, ["quant.ops.sleeve_health", "--check"], False),
]


def plist(label, hour, minute, args, weekdays_only):
    prog = "".join(f"    <string>{a}</string>\n" for a in [PY, "-m", *args])
    if weekdays_only:
        cal = "".join(
            f"    <dict><key>Weekday</key><integer>{wd}</integer>"
            f"<key>Hour</key><integer>{hour}</integer>"
            f"<key>Minute</key><integer>{minute}</integer></dict>\n"
            for wd in range(1, 6))
        cal = f"  <key>StartCalendarInterval</key>\n  <array>\n{cal}  </array>"
    else:
        cal = (f"  <key>StartCalendarInterval</key>\n  <dict>"
               f"<key>Hour</key><integer>{hour}</integer>"
               f"<key>Minute</key><integer>{minute}</integer></dict>")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.quant.{label}</string>
  <key>ProgramArguments</key>
  <array>
{prog}  </array>
  <key>WorkingDirectory</key><string>{REPO}</string>
{cal}
  <key>StandardOutPath</key><string>{LOG}/sched-{label}.log</string>
  <key>StandardErrorPath</key><string>{LOG}/sched-{label}.err</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
"""


def install():
    os.makedirs(LA, exist_ok=True)
    os.makedirs(LOG, exist_ok=True)
    for label, h, m, args, wd in JOBS:
        p = os.path.join(LA, f"com.quant.{label}.plist")
        with open(p, "w") as f:
            f.write(plist(label, h, m, args, wd))
        subprocess.run(["launchctl", "unload", p],
                       capture_output=True)
        r = subprocess.run(["launchctl", "load", p], capture_output=True,
                           text=True)
        status = "OK" if r.returncode == 0 else f"FEHLER: {r.stderr.strip()}"
        when = "täglich" if not wd else "Mo-Fr"
        print(f"com.quant.{label:12s} {h:02d}:{m:02d} {when:8s} {status}")
    # alte kombinierte dailyops-Plist ablösen (jetzt _run_daily)
    old = os.path.join(LA, "com.quant.dailyops.plist")
    print("\nAlle Jobs geladen. Prüfen: launchctl list | grep quant")


def uninstall():
    for label, *_ in JOBS:
        p = os.path.join(LA, f"com.quant.{label}.plist")
        if os.path.exists(p):
            subprocess.run(["launchctl", "unload", p], capture_output=True)
            os.remove(p)
            print(f"entladen: com.quant.{label}")


def show():
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "quant" in line:
            print(line)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.install:
        install()
    elif a.uninstall:
        uninstall()
    elif a.list:
        show()
    else:
        ap.print_help()
        sys.exit(1)
