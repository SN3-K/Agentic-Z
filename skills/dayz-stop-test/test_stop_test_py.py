"""Tests for stop_test.py (the Linux /dayz-stop-test). The process list and os.kill are faked."""
import errno
import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("dayz_stop_test_py", Path(__file__).resolve().parent / "stop_test.py")
st = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(st)
pe = st.platform_env

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="on Windows stop_test.py hands off to stop_test.bat")


@pytest.fixture
def procs(monkeypatch):
    """running: pid -> command line of the DayZDiag processes; kills records the signalled pids."""
    state = {"running": {}, "kills": [], "deny": set()}

    def listing(image, proc=None):
        assert image == "DayZDiag_x64.exe"
        return sorted(state["running"].items())

    def kill(pid, sig):
        if pid in state["deny"]:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        state["kills"].append(pid)
        state["running"].pop(pid)

    monkeypatch.setattr(pe, "posix_processes", listing)
    monkeypatch.setattr(pe.os, "kill", kill)
    return state


@pytest.mark.parametrize("flag", ["--help", "-h", "--HELP"])
def test_help_prints_usage_and_never_kills(procs, capsys, flag):
    procs["running"] = {10: r"Z:\games\DayZ\DayZDiag_x64.exe -server"}
    assert st.main([flag]) == 0
    assert "Usage: stop_test.py" in capsys.readouterr().out
    assert procs["kills"] == []


def test_unknown_argument_is_refused_without_killing(procs, capsys):
    procs["running"] = {10: "DayZDiag_x64.exe"}
    assert st.main(["chernarus"]) == 2
    assert "Unknown argument: chernarus" in capsys.readouterr().err
    assert procs["kills"] == []


def test_kills_every_diag_process(procs, capsys):
    procs["running"] = {10: r"Z:\games\DayZ\DayZDiag_x64.exe -server", 11: r"C:\DayZ\DayZDiag_x64.exe"}
    assert st.main([]) == 0
    assert procs["kills"] == [10, 11]
    out = capsys.readouterr().out
    assert 'with PID 10 has been terminated' in out and 'with PID 11 has been terminated' in out


def test_no_process_exits_0(procs, capsys):
    assert st.main([]) == 0
    assert "No DayZDiag_x64.exe processes running" in capsys.readouterr().out


def test_kill_refused_exits_1_with_the_reason(procs, capsys):
    procs["running"] = {10: "DayZDiag_x64.exe", 11: "DayZDiag_x64.exe"}
    procs["deny"] = {11}
    assert st.main([]) == 1
    captured = capsys.readouterr()
    assert "PID 10 has been terminated" in captured.out
    assert "Could not kill DayZDiag_x64.exe PID 11: Operation not permitted" in captured.err


def test_unreadable_process_list_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(pe, "posix_processes", lambda image, proc=None: None)
    assert st.main([]) == 1
    assert "Could not read the process list" in capsys.readouterr().err
