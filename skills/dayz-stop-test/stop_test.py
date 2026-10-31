"""/dayz-stop-test on Linux: force-kill every DayZDiag_x64.exe (server and client).

DayZDiag runs under Proton there, so the processes are found by the program
name in /proc/<pid>/cmdline (Wine shows it as the Windows path) and killed
with SIGKILL, the equivalent of taskkill /F. No preflight gate: abort-only
skills are the documented exception. On Windows this hands off to
stop_test.bat, which does the same with taskkill.

Exit codes match stop_test.bat: 0 when the processes are gone or none were
running, 2 for an argument (it takes none), and 1 when a process could not be
killed or the process list could not be read.

Run:
    python skills/dayz-stop-test/stop_test.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import platform_env  # noqa: E402

DIAG_EXE = "DayZDiag_x64.exe"
BAT = Path(__file__).resolve().parent / "stop_test.bat"
USAGE = (
    "Usage: stop_test.py\n"
    f"  Force-kills every running {DIAG_EXE} process (the local test server and client).\n"
    "  Takes no arguments. Exits 0 when the processes are gone or none were running."
)


def main(argv: list[str]) -> int:
    if argv:
        if argv[0].lower() in ("--help", "-h"):
            print(USAGE)
            return 0
        print(f"[FAIL] Unknown argument: {argv[0]} (stop-test takes no arguments; see --help).", file=sys.stderr)
        return 2
    if platform_env.IS_WINDOWS:
        return subprocess.call(["cmd", "/c", str(BAT)])
    if platform_env.posix_processes(DIAG_EXE) is None:
        print("[FAIL] Could not read the process list (/proc).", file=sys.stderr)
        return 1
    killed, errors = platform_env.kill_posix_processes(DIAG_EXE)
    for pid in killed:
        print(f'SUCCESS: The process "{DIAG_EXE}" with PID {pid} has been terminated.')
    for err in errors:
        print(f"[FAIL] Could not kill {DIAG_EXE} {err}", file=sys.stderr)
    if errors:
        return 1
    if not killed:
        print(f"[OK]   No {DIAG_EXE} processes running.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
