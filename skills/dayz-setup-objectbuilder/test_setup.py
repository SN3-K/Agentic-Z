"""Tests for setup.apply_preferences with a fake winreg (no real registry access)."""
from __future__ import annotations

import importlib.util
import shlex
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load_setup():
    spec = importlib.util.spec_from_file_location("dayz_setup_objectbuilder", HERE / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = _load_setup()


class _FakeKey:
    def __init__(self, store: dict):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_winreg(store: dict, writes: list):
    mod = types.ModuleType("winreg")
    mod.HKEY_CURRENT_USER = object()
    mod.KEY_READ = 1
    mod.KEY_SET_VALUE = 2
    mod.REG_SZ = 1

    def open_key(hive, subkey, reserved=0, access=0):
        assert hive is mod.HKEY_CURRENT_USER
        assert subkey == setup.REG_SUBKEY_CCONFIG
        return _FakeKey(store)

    def query_value_ex(key, name):
        if name not in key.store:
            raise OSError("missing")
        return key.store[name], mod.REG_SZ

    def set_value_ex(key, name, reserved, kind, value):
        assert kind == mod.REG_SZ
        key.store[name] = value
        writes.append((name, value))

    mod.OpenKey = open_key
    mod.QueryValueEx = query_value_ex
    mod.SetValueEx = set_value_ex
    return mod


@pytest.fixture
def fake_registry(monkeypatch):
    store: dict = {}
    writes: list = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(store, writes))
    return store, writes


def test_writes_all_four_flags_when_absent(fake_registry):
    store, writes = fake_registry
    assert setup.apply_preferences() == 4
    assert len(writes) == 4
    for name, (on_value, _label) in setup.PREFERRED_FLAGS_ON.items():
        assert store[name] == on_value


def test_direct3d_flag_uses_on_string(fake_registry):
    store, _ = fake_registry
    setup.apply_preferences()
    assert store["#Use Direct3D renderer by default"] == "On"
    assert store["#View background LOD"] == "1"


def test_skips_values_already_on(fake_registry):
    store, writes = fake_registry
    store.update({name: on for name, (on, _l) in setup.PREFERRED_FLAGS_ON.items()})
    assert setup.apply_preferences() == 0
    assert writes == []


def test_rewrites_flags_switched_off(fake_registry):
    store, writes = fake_registry
    store.update({name: on for name, (on, _l) in setup.PREFERRED_FLAGS_ON.items()})
    store["#View background LOD"] = "0"
    store["#Use Direct3D renderer by default"] = "1"  # wrong "on" form for this flag
    assert setup.apply_preferences() == 2
    assert sorted(n for n, _ in writes) == ["#Use Direct3D renderer by default", "#View background LOD"]


# --- .reg safety check --------------------------------------------------------

HKCU_REG = (
    "Windows Registry Editor Version 5.00\r\n\r\n"
    "[HKEY_CURRENT_USER\\Software\\Bohemia Interactive\\DayZ Tools\\Object Builder\\CConfig]\r\n"
    '"#View background LOD"="0"\r\n\r\n'
    "[-HKEY_CURRENT_USER\\Software\\Bohemia Interactive\\DayZ Tools\\Object Builder\\Old]\r\n"
)


def test_hkcu_only_reg_passes_utf16(tmp_path):
    reg = tmp_path / "defaults.reg"
    reg.write_bytes(b"\xff\xfe" + HKCU_REG.encode("utf-16-le"))
    assert setup.foreign_reg_keys(reg) == []


def test_reg_touching_hklm_is_reported(tmp_path):
    reg = tmp_path / "defaults.reg"
    reg.write_text(HKCU_REG + "[HKEY_LOCAL_MACHINE\\SOFTWARE\\Evil]\r\n", encoding="utf-8")
    assert setup.foreign_reg_keys(reg) == ["HKEY_LOCAL_MACHINE\\SOFTWARE\\Evil"]


def test_reg_without_keys_is_reported(tmp_path):
    reg = tmp_path / "defaults.reg"
    reg.write_text("Windows Registry Editor Version 5.00\r\n", encoding="utf-8")
    assert setup.foreign_reg_keys(reg) == ["<no registry keys found>"]


# --- main() -------------------------------------------------------------------

class _FakePreflight:
    def __init__(self, root):
        self.root = root

    def find_dayz_tools(self):
        return self.root


@pytest.fixture
def env(tmp_path, monkeypatch):
    """DayZ Tools tree in tmp_path with ObjectBuilder.exe and an HKCU-only .reg.

    Registry access, reg import and the preflight gate are all faked; `state`
    records what happened.
    """
    ob = tmp_path / "Bin" / "ObjectBuilder"
    ob.mkdir(parents=True)
    (ob / "ObjectBuilder.exe").write_bytes(b"")
    (ob / "ObjectBuilder_defaultSettings.reg").write_text(HKCU_REG, encoding="utf-8")
    state = {"configured": False, "import_rc": 0, "import_creates_keys": True, "imports": 0, "prefs": 0}

    def fake_import(path):
        state["imports"] += 1
        state["order"].append("import")
        if state["import_rc"] == 0 and state["import_creates_keys"]:
            state["configured"] = True
        return state["import_rc"]

    monkeypatch.setattr(setup.h, "gate_on_preflight", lambda: None)
    monkeypatch.setattr(setup.h, "preflight", _FakePreflight(tmp_path), raising=False)
    monkeypatch.setattr(setup, "registry_has_objectbuilder_settings", lambda: state["configured"])
    monkeypatch.setattr(setup, "import_reg_file", fake_import)
    def fake_prefs():
        state["prefs"] += 1
        return 1

    def fake_backup(backup_dir):
        state["backups"] += 1
        state["order"].append("backup")
        return None if state["backup_fails"] else backup_dir / "objectbuilder-backup.reg"

    def fake_delete():
        state["order"].append("delete")
        if state["delete_rc"] == 0:
            state["configured"] = False
            state["partial"] = False
        return state["delete_rc"]

    # "partial": the parent Object Builder key exists without CConfig.
    state.update(backups=0, backup_fails=False, delete_rc=0, order=[], running=False, partial=False)
    monkeypatch.setattr(setup, "registry_has_objectbuilder_key",
                        lambda: state["configured"] or state["partial"])
    monkeypatch.setattr(setup, "objectbuilder_state", lambda: (state["running"], 0))
    monkeypatch.setattr(setup, "delete_objectbuilder_key", fake_delete)
    monkeypatch.setattr(setup, "apply_preferences", fake_prefs)
    monkeypatch.setattr(setup, "backup_registry", fake_backup)
    monkeypatch.setattr(setup, "BACKUP_DIR", tmp_path / "local-memory")
    # restore_hint on Linux looks up the prefix and P root; keep it off the real Steam folders.
    monkeypatch.setattr(setup, "tools_prefix", lambda: None)
    monkeypatch.setenv("DAYZ_P_ROOT", str(tmp_path / "P"))
    return ob, state


def test_main_fresh_machine_imports_and_applies(env, capsys):
    _, state = env
    assert setup.main([]) == 0
    assert state["imports"] == 1 and state["prefs"] == 1
    assert "[INFO]  1 of 4 preferences changed." in capsys.readouterr().out


def test_main_already_configured_skips_import(env):
    _, state = env
    state["configured"] = True
    assert setup.main([]) == 0
    assert state["imports"] == 0 and state["prefs"] == 1


@pytest.mark.parametrize("configured, argv", [(False, []), (True, []), (True, ["--force", "--yes"])])
def test_main_refuses_while_objectbuilder_runs(env, capsys, configured, argv):
    _, state = env
    state.update(running=True, configured=configured)
    assert setup.main(argv) == 1
    assert "Close Object Builder first" in capsys.readouterr().err
    assert state["imports"] == 0 and state["prefs"] == 0 and state["order"] == []


@pytest.mark.parametrize("stdout, running", [
    ("ObjectBuilder.exe             1234 Console    1     85,000 K\n", True),
    ("INFO: No tasks are running which match the specified criteria.\n", False),
])
def test_objectbuilder_running_reads_tasklist(monkeypatch, stdout, running):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    assert setup.objectbuilder_running() is running
    assert setup.objectbuilder_state() == (running, 0)
    assert calls == [["tasklist", "/FI", "IMAGENAME eq ObjectBuilder.exe", "/NH"]] * 2


def test_objectbuilder_state_unknown_when_tasklist_fails(monkeypatch):
    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(setup.subprocess, "run",
                        lambda cmd, **kw: types.SimpleNamespace(returncode=5, stdout="", stderr="ERROR"))
    assert setup.objectbuilder_state() == (None, 5)


@pytest.mark.parametrize("configured, argv", [(False, []), (True, []), (True, ["--force", "--yes"])])
def test_main_refuses_when_tasklist_fails(env, capsys, monkeypatch, configured, argv):
    """A failed tasklist must not be read as 'not running': an open Object Builder
    would write its old settings back on exit and undo the reset."""
    _, state = env
    state["configured"] = configured
    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(setup, "objectbuilder_state", lambda: (None, 5))
    assert setup.main(argv) == 1
    err = capsys.readouterr().err
    assert "tasklist failed (exit code 5)" in err and "Close Object Builder" in err
    assert state["imports"] == 0 and state["prefs"] == 0 and state["order"] == []


def test_main_missing_exe_fails(env, capsys):
    ob, state = env
    (ob / "ObjectBuilder.exe").unlink()
    assert setup.main([]) == 1
    assert "ObjectBuilder.exe not found" in capsys.readouterr().err
    assert state["imports"] == 0


def test_main_missing_reg_fails(env, capsys):
    ob, state = env
    (ob / "ObjectBuilder_defaultSettings.reg").unlink()
    assert setup.main([]) == 1
    assert "Default settings .reg missing" in capsys.readouterr().err
    assert state["imports"] == 0


def test_main_reg_import_failure_returns_its_code(env, capsys):
    _, state = env
    state["import_rc"] = 5
    assert setup.main([]) == 5
    assert "reg import failed (exit 5)" in capsys.readouterr().err
    assert state["prefs"] == 0


def test_main_keys_missing_after_import_fails(env, capsys):
    _, state = env
    state["import_creates_keys"] = False
    assert setup.main([]) == 1
    assert "keys are still missing" in capsys.readouterr().err
    assert state["prefs"] == 0


def test_main_refuses_reg_outside_hkcu(env, capsys):
    ob, state = env
    (ob / "ObjectBuilder_defaultSettings.reg").write_text(
        HKCU_REG + "[HKEY_LOCAL_MACHINE\\SOFTWARE\\X]\r\n", encoding="utf-8"
    )
    assert setup.main([]) == 1
    assert "outside HKEY_CURRENT_USER" in capsys.readouterr().err
    assert state["imports"] == 0


def test_main_force_without_tty_needs_yes(env, monkeypatch, capsys):
    _, state = env
    state["configured"] = True
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert setup.main(["--force"]) == 1
    assert "--force --yes" in capsys.readouterr().err
    assert state["imports"] == 0


def test_main_force_with_yes_reimports(env):
    _, state = env
    state["configured"] = True
    assert setup.main(["--force", "--yes"]) == 0
    assert state["imports"] == 1


@pytest.mark.parametrize("answer, rc, imports", [("y", 0, 1), ("n", 1, 0), ("", 1, 0)])
def test_main_force_prompts_on_tty(env, monkeypatch, answer, rc, imports):
    _, state = env
    state["configured"] = True
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert setup.main(["--force"]) == rc
    assert state["imports"] == imports


def test_main_force_backs_up_before_import(env, capsys):
    _, state = env
    state["configured"] = True
    assert setup.main(["--force", "--yes"]) == 0
    assert state["backups"] == 1 and state["imports"] == 1
    assert "backed up to" in capsys.readouterr().out


def test_main_force_aborts_when_backup_fails(env, capsys):
    _, state = env
    state["configured"] = True
    state["backup_fails"] = True
    assert setup.main(["--force", "--yes"]) == 1
    assert "reset not attempted" in capsys.readouterr().err
    assert state["imports"] == 0
    assert "delete" not in state["order"]


def test_main_force_deletes_key_after_backup_and_before_import(env, capsys):
    _, state = env
    state["configured"] = True
    assert setup.main(["--force", "--yes"]) == 0
    assert state["order"] == ["backup", "delete", "import"]
    assert "Deleted HKCU" in capsys.readouterr().out


def test_main_force_stops_when_delete_fails(env, capsys):
    _, state = env
    state["configured"] = True
    state["delete_rc"] = 1
    assert setup.main(["--force", "--yes"]) == 1
    assert "Could not delete" in capsys.readouterr().err
    assert state["order"] == ["backup", "delete"]


def _path_aware_import(monkeypatch, state, defaults_rc, defaults_creates_keys=True, backup_rc=0):
    """Fake `reg import` whose result depends on which file is imported."""
    imported = []

    def fake_import(path):
        imported.append(Path(path).name)
        state["order"].append("import")
        is_backup = Path(path).name == "objectbuilder-backup.reg"
        rc = backup_rc if is_backup else defaults_rc
        if rc == 0 and (is_backup or defaults_creates_keys):
            state["configured"] = True
        return rc

    monkeypatch.setattr(setup, "import_reg_file", fake_import)
    return imported


def test_main_force_restores_backup_when_import_fails_after_delete(env, monkeypatch, capsys):
    _, state = env
    state["configured"] = True
    imported = _path_aware_import(monkeypatch, state, defaults_rc=5)
    assert setup.main(["--force", "--yes"]) == 5
    assert imported == ["ObjectBuilder_defaultSettings.reg", "objectbuilder-backup.reg"]
    assert state["order"] == ["backup", "delete", "import", "delete", "import"]
    assert state["configured"] is True and state["prefs"] == 0
    captured = capsys.readouterr()
    assert "reg import failed (exit 5)" in captured.err
    assert "Previous settings restored" in captured.out


def test_main_force_restores_backup_when_keys_missing_after_import(env, monkeypatch, capsys):
    _, state = env
    state["configured"] = True
    imported = _path_aware_import(monkeypatch, state, defaults_rc=0, defaults_creates_keys=False)
    assert setup.main(["--force", "--yes"]) == 1
    assert imported == ["ObjectBuilder_defaultSettings.reg", "objectbuilder-backup.reg"]
    assert state["configured"] is True
    assert "Previous settings restored" in capsys.readouterr().out


def test_main_force_reports_manual_restore_when_restore_fails(env, monkeypatch, capsys):
    _, state = env
    state["configured"] = True
    _path_aware_import(monkeypatch, state, defaults_rc=5, backup_rc=7)
    monkeypatch.setattr(setup.sys, "platform", "win32")
    assert setup.main(["--force", "--yes"]) == 5
    err = capsys.readouterr().err
    assert "Restore failed (exit 7)" in err
    backup = setup.BACKUP_DIR / "objectbuilder-backup.reg"
    assert f'Restore by hand with: reg import "{backup}"' in err


def test_main_fresh_import_failure_does_not_try_a_restore(env, monkeypatch):
    _, state = env
    imported = _path_aware_import(monkeypatch, state, defaults_rc=5)
    assert setup.main([]) == 5
    assert imported == ["ObjectBuilder_defaultSettings.reg"]
    assert state["order"] == ["import"]


def test_backup_dir_is_the_project_found_from_a_subfolder(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".claude" / "local-memory").mkdir(parents=True)
    (project / ".claude" / "local-memory" / "dayz-current-project.txt").write_text("x", encoding="utf-8")
    sub = project / "workspace" / "MyMod"
    sub.mkdir(parents=True)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(sub)
    fresh = _load_setup()
    assert fresh.PROJECT_DIR == project.resolve()
    assert fresh.BACKUP_DIR == project.resolve() / ".claude" / "local-memory"


def test_main_force_declined_deletes_nothing(env, monkeypatch):
    _, state = env
    state["configured"] = True
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert setup.main(["--force"]) == 1
    assert state["order"] == []


def test_main_force_refuses_foreign_reg_before_touching_registry(env, capsys):
    ob, state = env
    state["configured"] = True
    (ob / "ObjectBuilder_defaultSettings.reg").write_text(
        HKCU_REG + "[HKEY_LOCAL_MACHINE\\SOFTWARE\\X]\r\n", encoding="utf-8"
    )
    assert setup.main(["--force", "--yes"]) == 1
    assert "outside HKEY_CURRENT_USER" in capsys.readouterr().err
    assert state["order"] == []


def test_main_fresh_machine_does_not_delete(env):
    _, state = env
    assert setup.main([]) == 0
    assert state["order"] == ["import"]


def test_main_fresh_machine_skips_backup(env):
    _, state = env
    assert setup.main([]) == 0
    assert state["backups"] == 0


# --- backup_registry with a mocked reg export ----------------------------------

def test_backup_registry_runs_reg_export(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.sys, "platform", "win32")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[3]).write_text("exported", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    dest = setup.backup_registry(tmp_path / "mem")
    assert dest is not None and dest.is_file()
    assert dest.parent == tmp_path / "mem"
    assert dest.name.startswith("objectbuilder-backup-") and dest.suffix == ".reg"
    assert calls == [["reg", "export", "HKCU\\" + setup.REG_KEY, str(dest), "/y"]]


def test_delete_objectbuilder_key_runs_reg_delete(monkeypatch):
    monkeypatch.setattr(setup.sys, "platform", "win32")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    assert setup.delete_objectbuilder_key() == 0
    assert calls == [["reg", "delete", "HKCU\\" + setup.REG_KEY, "/f"]]


def test_delete_objectbuilder_key_failure_returns_code(monkeypatch, capsys):
    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(
        setup.subprocess, "run",
        lambda cmd, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="ERROR: access denied"),
    )
    assert setup.delete_objectbuilder_key() == 1
    assert "access denied" in capsys.readouterr().err


def test_backup_registry_failure_returns_none(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(
        setup.subprocess, "run",
        lambda cmd, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="ERROR: access denied"),
    )
    assert setup.backup_registry(tmp_path) is None
    assert "access denied" in capsys.readouterr().err


# --- --no-prefs, preference write errors, partial trees ------------------------

@pytest.mark.parametrize("configured", [False, True])
def test_main_no_prefs_leaves_flags_alone(env, capsys, configured):
    _, state = env
    state["configured"] = configured
    assert setup.main(["--no-prefs"]) == 0
    assert state["prefs"] == 0
    assert "--no-prefs: preferred flags left as they are" in capsys.readouterr().out


def test_main_default_run_still_applies_prefs_and_names_opt_out(env, capsys):
    _, state = env
    state["configured"] = True
    assert setup.main([]) == 0
    assert state["prefs"] == 1
    assert "skip with --no-prefs" in capsys.readouterr().out


def test_main_returns_1_when_preferences_fail(env, monkeypatch):
    _, state = env
    state["configured"] = True
    monkeypatch.setattr(setup, "apply_preferences", lambda: None)
    assert setup.main([]) == 1


def _raising_winreg(monkeypatch, fail_on):
    store: dict = {}
    mod = _fake_winreg(store, [])

    def boom(*a, **k):
        raise PermissionError(5, "Access is denied")

    if fail_on == "open":
        mod.OpenKey = boom
    else:
        mod.SetValueEx = boom
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", mod)


@pytest.mark.parametrize("fail_on", ["open", "set"])
def test_apply_preferences_reports_oserror_cleanly(monkeypatch, capsys, fail_on):
    _raising_winreg(monkeypatch, fail_on)
    assert setup.apply_preferences() is None
    err = capsys.readouterr().err
    assert "[FAIL]" in err and "Could not write preferences" in err and "Access is denied" in err
    assert "--no-prefs" in err


_REAL_APPLY_PREFERENCES = setup.apply_preferences  # captured before the env fixture fakes it


def test_main_preference_oserror_exits_1_without_traceback(env, monkeypatch, capsys):
    _, state = env
    state["configured"] = True
    monkeypatch.setattr(setup, "apply_preferences", _REAL_APPLY_PREFERENCES)
    _raising_winreg(monkeypatch, "open")
    assert setup.main([]) == 1
    assert "Could not write preferences" in capsys.readouterr().err


def test_main_partial_tree_is_backed_up_then_merged(env, capsys):
    _, state = env
    state["partial"] = True
    assert setup.main([]) == 0
    assert state["order"] == ["backup", "import"]
    assert "backed up to" in capsys.readouterr().out


def test_main_partial_tree_backup_failure_stops_before_import(env, capsys):
    _, state = env
    state.update(partial=True, backup_fails=True)
    assert setup.main([]) == 1
    assert "defaults not imported" in capsys.readouterr().err
    assert state["order"] == ["backup"] and state["prefs"] == 0


def test_main_partial_tree_restores_backup_when_import_fails(env, monkeypatch, capsys):
    _, state = env
    state["partial"] = True
    imported = []

    def fake_import(path):
        name = Path(path).name
        imported.append(name)
        state["order"].append("import")
        if name == "objectbuilder-backup.reg":
            state["partial"] = True  # the backup never had CConfig
            return 0
        return 5

    monkeypatch.setattr(setup, "import_reg_file", fake_import)
    assert setup.main([]) == 5
    assert imported == ["ObjectBuilder_defaultSettings.reg", "objectbuilder-backup.reg"]
    assert state["order"] == ["backup", "import", "delete", "import"]
    assert state["partial"] is True and state["configured"] is False
    assert "Previous settings restored" in capsys.readouterr().out


def test_main_empty_registry_takes_no_backup(env):
    _, state = env
    assert setup.main([]) == 0
    assert state["backups"] == 0 and state["order"] == ["import"]


def test_registry_key_checks_use_the_right_subkeys(monkeypatch):
    present: set = set()
    mod = types.ModuleType("winreg")
    mod.HKEY_CURRENT_USER = object()

    def open_key(hive, subkey, *a):
        if subkey not in present:
            raise FileNotFoundError(subkey)
        return _FakeKey({})

    mod.OpenKey = open_key
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", mod)
    assert not setup.registry_has_objectbuilder_key()
    present.add(setup.REG_KEY)
    assert setup.registry_has_objectbuilder_key()
    assert not setup.registry_has_objectbuilder_settings()
    present.add(setup.REG_SUBKEY_CCONFIG)
    assert setup.registry_has_objectbuilder_settings()


# --- Linux: the DayZ Tools Proton prefix's registry -----------------------------

_REAL_OBJECTBUILDER_STATE = setup.objectbuilder_state  # captured before the env fixture fakes it

# to_wine_path only converts on a Linux host.
linux_paths = pytest.mark.skipif(sys.platform == "win32", reason="Z:\\ conversion happens on Linux only")

CCONFIG_VALUES = {"#View background LOD": "0", "#Use Direct3D renderer by default": "On"}

# What Wine's `reg query` prints for a key: its full name, then one line per value
# (four-space separators), then the full names of its subkeys.
REG_QUERY_OUTPUT = (
    "\r\n"
    "HKEY_CURRENT_USER\\Software\\Bohemia Interactive\\DayZ Tools\\Object Builder\\CConfig\r\n"
    "    #View background LOD    REG_SZ    0\r\n"
    "    #Show background LOD in UV (by default)    REG_SZ    1\r\n"
    "    Empty value    REG_SZ    \r\n"
    "    Number    REG_DWORD    0x1\r\n"
    "    Path    REG_SZ    C:\\path with  spaces\\x\r\n"
    "\r\n"
    "HKEY_CURRENT_USER\\Software\\Bohemia Interactive\\DayZ Tools\\Object Builder\\CConfig\\Sub\r\n"
)


@pytest.fixture
def wine_prefix(tmp_path, monkeypatch):
    """A fake DayZ Tools prefix folder on the Linux branch; its registry is faked per test."""
    pfx = tmp_path / "pfx"
    pfx.mkdir()
    monkeypatch.setattr(setup.sys, "platform", "linux")
    monkeypatch.setattr(setup, "tools_prefix", lambda: pfx)
    monkeypatch.setenv("DAYZ_P_ROOT", str(tmp_path / "P"))
    return pfx


def _fake_reg_exe(monkeypatch, rc=0, output="", creates=None):
    """Fake h.run_windows_tool; returns the list of (exe, args) it was called with."""
    calls = []

    def fake_run(exe, args, *, cwd=None, timeout=None):
        calls.append((exe, args))
        if creates is not None:
            creates.write_text("exported", encoding="utf-8")
        return rc, output

    monkeypatch.setattr(setup.h, "run_windows_tool", fake_run)
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: pytest.fail("ran the Windows reg on Linux"))
    return calls


def _fake_registry(monkeypatch, keys, add_rc=0, add_output=""):
    """Fake the prefix's reg.exe over keys ({subkey: {name: data}}): `reg query` answers as
    Wine does, `reg add` returns add_rc. Returns the list of argument lists it was called with."""
    calls = []

    def fake_run(exe, args, *, cwd=None, timeout=None):
        calls.append(args)
        if args[0] == "add":
            return add_rc, add_output
        assert args[0] == "query" and len(args) == 2
        subkey = args[1].removeprefix("HKCU\\")
        if subkey not in keys:
            return 1, "reg: Unable to find the specified registry key\r\n"
        lines = [f"HKEY_CURRENT_USER\\{subkey}"]
        lines += [f"    {name}    REG_SZ    {data}" for name, data in keys[subkey].items()]
        return 0, "\r\n" + "\r\n".join(lines) + "\r\n"

    monkeypatch.setattr(setup.h, "run_windows_tool", fake_run)
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: pytest.fail("ran the Windows reg on Linux"))
    return calls


def test_parse_reg_query_reads_every_value_line():
    assert setup._parse_reg_query(REG_QUERY_OUTPUT) == {
        "#View background LOD": "0",
        "#Show background LOD in UV (by default)": "1",
        "Empty value": "",
        "Number": "0x1",
        "Path": "C:\\path with  spaces\\x",
    }


def test_parse_reg_query_of_a_key_without_values_is_empty():
    output = "\r\nHKEY_CURRENT_USER\\Software\\X\r\n\r\nHKEY_CURRENT_USER\\Software\\X\\Y\r\n"
    assert setup._parse_reg_query(output) == {}


def test_linux_key_checks_run_reg_query_in_the_prefix(wine_prefix, monkeypatch):
    seen = []

    def fake_run(exe, args, *, cwd=None, timeout=None):
        seen.append((exe, args))
        return 0, REG_QUERY_OUTPUT

    monkeypatch.setattr(setup.h, "run_windows_tool", fake_run)
    assert setup.registry_has_objectbuilder_settings()
    assert setup.registry_has_objectbuilder_key()
    reg_exe = wine_prefix / "drive_c" / "windows" / "system32" / "reg.exe"
    assert seen == [(reg_exe, ["query", "HKCU\\" + setup.REG_SUBKEY_CCONFIG]),
                    (reg_exe, ["query", "HKCU\\" + setup.REG_KEY])]


def test_linux_key_missing(wine_prefix, monkeypatch):
    _fake_registry(monkeypatch, {})
    assert not setup.registry_has_objectbuilder_settings()
    assert not setup.registry_has_objectbuilder_key()


def test_linux_parent_key_without_cconfig_is_a_partial_tree(wine_prefix, monkeypatch):
    _fake_registry(monkeypatch, {setup.REG_KEY: {}})
    assert setup.registry_has_objectbuilder_key()
    assert not setup.registry_has_objectbuilder_settings()


def test_linux_wine_reg_query_returns_the_values(wine_prefix, monkeypatch):
    _fake_registry(monkeypatch, {setup.REG_SUBKEY_CCONFIG: CCONFIG_VALUES})
    assert setup._wine_reg_query(setup.REG_SUBKEY_CCONFIG) == CCONFIG_VALUES
    assert setup._wine_reg_query(setup.REG_KEY) is None


def test_linux_key_check_without_proton_is_unknown_not_missing(wine_prefix, monkeypatch):
    def no_proton(exe, args, *, cwd=None, timeout=None):
        raise setup.h.ProtonError("no Proton found for DayZ Tools.")

    monkeypatch.setattr(setup.h, "run_windows_tool", no_proton)
    with pytest.raises(setup.RegistryUnreadable, match="Could not run reg.exe through Proton: no Proton found"):
        setup.registry_has_objectbuilder_settings()


def test_linux_key_check_without_prefix_is_unknown_not_missing(wine_prefix, monkeypatch):
    monkeypatch.setattr(setup, "tools_prefix", lambda: None)
    monkeypatch.setattr(setup.h, "run_windows_tool", lambda *a, **k: pytest.fail("ran reg.exe without a prefix"))
    with pytest.raises(setup.RegistryUnreadable, match="compatdata/830640"):
        setup.registry_has_objectbuilder_key()


@linux_paths
def test_linux_reg_import_runs_the_prefix_reg_exe(wine_prefix, tmp_path, monkeypatch, capsys):
    calls = _fake_reg_exe(monkeypatch, output="The operation completed successfully.")
    reg = tmp_path / "defaults.reg"
    assert setup.import_reg_file(reg) == 0
    reg_exe = wine_prefix / "drive_c" / "windows" / "system32" / "reg.exe"
    assert calls == [(reg_exe, ["import", "Z:" + str(reg).replace("/", "\\")])]
    assert "completed successfully" in capsys.readouterr().out


def test_linux_reg_import_failure_returns_code_and_output(wine_prefix, tmp_path, monkeypatch, capsys):
    _fake_reg_exe(monkeypatch, rc=1, output="reg: Unable to open the file")
    assert setup.import_reg_file(tmp_path / "defaults.reg") == 1
    assert "Unable to open the file" in capsys.readouterr().err


@linux_paths
def test_linux_backup_runs_reg_export_with_a_z_path(wine_prefix, tmp_path, monkeypatch):
    backup_dir = tmp_path / "mem"
    seen = {}

    def fake_run(exe, args, *, cwd=None, timeout=None):
        seen["args"] = args
        dest = backup_dir / args[2].rsplit("\\", 1)[1]
        dest.write_text("exported", encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(setup.h, "run_windows_tool", fake_run)
    dest = setup.backup_registry(backup_dir)
    assert dest is not None and dest.is_file()
    assert seen["args"] == ["export", "HKCU\\" + setup.REG_KEY, "Z:" + str(dest).replace("/", "\\"), "/y"]


def test_linux_backup_failure_returns_none(wine_prefix, tmp_path, monkeypatch, capsys):
    _fake_reg_exe(monkeypatch, rc=1, output="reg: Unable to find the specified registry key")
    assert setup.backup_registry(tmp_path / "mem") is None
    assert "Unable to find" in capsys.readouterr().err


def test_linux_delete_runs_reg_delete(wine_prefix, monkeypatch):
    calls = _fake_reg_exe(monkeypatch)
    assert setup.delete_objectbuilder_key() == 0
    assert [args for _, args in calls] == [["delete", "HKCU\\" + setup.REG_KEY, "/f"]]


def test_linux_proton_error_is_reported_as_a_failed_reg(wine_prefix, monkeypatch, capsys):
    def no_proton(exe, args, *, cwd=None, timeout=None):
        raise setup.h.ProtonError("no Proton found for DayZ Tools. Set DAYZ_PROTON to a Proton folder.")

    monkeypatch.setattr(setup.h, "run_windows_tool", no_proton)
    assert setup.delete_objectbuilder_key() == 1
    err = capsys.readouterr().err
    assert "Could not run reg.exe through Proton: no Proton found" in err and "DAYZ_PROTON" in err


def test_linux_missing_prefix_is_reported_as_a_failed_reg(wine_prefix, monkeypatch, capsys):
    monkeypatch.setattr(setup, "tools_prefix", lambda: None)
    monkeypatch.setattr(setup.h, "run_windows_tool", lambda *a, **k: pytest.fail("ran reg.exe without a prefix"))
    assert setup.delete_objectbuilder_key() == 1
    assert "compatdata/830640" in capsys.readouterr().err


def test_linux_preferences_write_only_what_differs_with_reg_add(wine_prefix, monkeypatch, capsys):
    calls = _fake_registry(monkeypatch, {setup.REG_SUBKEY_CCONFIG: CCONFIG_VALUES})
    assert setup.apply_preferences() == 3  # Direct3D is already "On"
    key = "HKCU\\" + setup.REG_SUBKEY_CCONFIG
    assert calls == [["query", key]] + [
        ["add", key, "/v", name, "/t", "REG_SZ", "/d", on_value, "/f"]
        for name, (on_value, _label) in setup.PREFERRED_FLAGS_ON.items()
        if name != "#Use Direct3D renderer by default"
    ]
    assert "Use Direct3D renderer by default: already on" in capsys.readouterr().out


def test_linux_preferences_all_on_writes_nothing(wine_prefix, monkeypatch):
    all_on = {name: on for name, (on, _l) in setup.PREFERRED_FLAGS_ON.items()}
    calls = _fake_registry(monkeypatch, {setup.REG_SUBKEY_CCONFIG: all_on})
    assert setup.apply_preferences() == 0
    assert [args[0] for args in calls] == ["query"]


def test_linux_preferences_reg_add_failure_is_reported(wine_prefix, monkeypatch, capsys):
    _fake_registry(monkeypatch, {setup.REG_SUBKEY_CCONFIG: CCONFIG_VALUES}, add_rc=1, add_output="reg: Access denied")
    assert setup.apply_preferences() is None
    err = capsys.readouterr().err
    assert "[FAIL]" in err and "reg add '#View background LOD' failed (exit 1)" in err
    assert "Access denied" in err and "0 of 4 were written" in err and "--no-prefs" in err


def test_linux_preferences_without_cconfig_fail_cleanly(wine_prefix, monkeypatch, capsys):
    calls = _fake_registry(monkeypatch, {})
    assert setup.apply_preferences() is None
    assert "the key does not exist in the DayZ Tools prefix" in capsys.readouterr().err
    assert [args[0] for args in calls] == ["query"]


def test_linux_preferences_when_reg_cannot_run_fail_cleanly(wine_prefix, monkeypatch, capsys):
    monkeypatch.setattr(setup, "tools_prefix", lambda: None)
    assert setup.apply_preferences() is None
    err = capsys.readouterr().err
    assert "Could not read preferences" in err and "compatdata/830640" in err and "--no-prefs" in err


# --- registry that cannot be read, and the restore hint -------------------------

def _unreadable():
    raise setup.RegistryUnreadable("Could not run reg.exe through Proton: no Proton found")


def test_main_stops_when_the_registry_cannot_be_read(env, monkeypatch, capsys):
    _, state = env
    monkeypatch.setattr(setup, "registry_has_objectbuilder_settings", _unreadable)
    assert setup.main([]) == 1
    err = capsys.readouterr().err
    assert "unknown whether Object Builder is configured. Nothing was changed." in err
    assert "no Proton found" in err and "not configured" not in err
    assert state["order"] == [] and state["prefs"] == 0


def test_main_reports_an_unchecked_import_with_the_restore_hint(env, monkeypatch, capsys):
    _, state = env
    state["configured"] = True
    checks = iter([True])  # the first check sees the old tree; the one after the import fails

    def settings():
        try:
            return next(checks)
        except StopIteration:
            _unreadable()

    monkeypatch.setattr(setup, "registry_has_objectbuilder_settings", settings)
    monkeypatch.setattr(setup, "restore_hint", lambda backup: f"RESTORE {backup.name}")
    assert setup.main(["--force", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "its keys could not be checked: Could not run reg.exe" in err
    assert "RESTORE objectbuilder-backup.reg" in err
    assert state["order"] == ["backup", "delete", "import"] and state["prefs"] == 0


def test_restore_backup_treats_an_unreadable_check_as_failed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "delete_objectbuilder_key", lambda: 0)
    monkeypatch.setattr(setup, "import_reg_file", lambda path: 0)
    monkeypatch.setattr(setup, "registry_has_objectbuilder_settings", _unreadable)
    monkeypatch.setattr(setup, "restore_hint", lambda backup: "RESTORE IT")
    assert setup.restore_backup(tmp_path / "b.reg") is False
    err = capsys.readouterr().err
    assert "Could not check the restored keys: Could not run reg.exe" in err
    assert "Restore by hand with: RESTORE IT" in err


def test_restore_hint_on_windows_is_reg_import(monkeypatch):
    monkeypatch.setattr(setup.sys, "platform", "win32")
    backup = Path("C:/proj/.claude/local-memory/objectbuilder-backup-1.reg")
    assert setup.restore_hint(backup) == f'reg import "{backup}"'


@linux_paths
def test_restore_hint_on_linux_is_the_proton_command(wine_prefix, tmp_path, monkeypatch):
    seen = {}

    def fake_cmd(exe, args):
        seen["exe"], seen["args"] = exe, args
        return ["/rt/_v2-entry-point", "--verb=run", "--", "/p/Proton 9.0 (Beta)/proton", "run", str(exe), *args], \
            {"STEAM_COMPAT_CLIENT_INSTALL_PATH": "/steam", "STEAM_COMPAT_DATA_PATH": "/lib/compat data/830640"}

    monkeypatch.setattr(setup.h, "windows_tool_cmd", fake_cmd)
    backup = tmp_path / "mem" / "objectbuilder-backup-1.reg"
    hint = setup.restore_hint(backup)
    wine_backup = "Z:" + str(backup).replace("/", "\\")
    reg_exe = wine_prefix / "drive_c" / "windows" / "system32" / "reg.exe"
    assert seen == {"exe": reg_exe, "args": ["import", wine_backup]}
    # The hint is one shell command: environment assignments, then the argv, each quoted.
    assert shlex.split(hint) == ["STEAM_COMPAT_CLIENT_INSTALL_PATH=/steam",
                                 "STEAM_COMPAT_DATA_PATH=/lib/compat data/830640",
                                 "/rt/_v2-entry-point", "--verb=run", "--", "/p/Proton 9.0 (Beta)/proton", "run",
                                 str(reg_exe), "import", wine_backup]
    assert "reg import" not in hint


@linux_paths
def test_restore_hint_on_linux_without_proton_names_the_file_and_reason(wine_prefix, tmp_path, monkeypatch):
    def no_proton(exe, args):
        raise setup.h.ProtonError("no Proton found for DayZ Tools.")

    monkeypatch.setattr(setup.h, "windows_tool_cmd", no_proton)
    backup = tmp_path / "objectbuilder-backup-1.reg"
    hint = setup.restore_hint(backup)
    assert hint == f'reg import "Z:{str(backup).replace("/", chr(92))}" in the DayZ Tools Proton prefix ' \
                   "(no Proton found for DayZ Tools.)"


@linux_paths
def test_restore_hint_on_linux_without_prefix(wine_prefix, tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "tools_prefix", lambda: None)
    monkeypatch.setattr(setup.h, "windows_tool_cmd", lambda *a: pytest.fail("built a command without a prefix"))
    hint = setup.restore_hint(tmp_path / "objectbuilder-backup-1.reg")
    assert hint.startswith('reg import "Z:\\') and "once DayZ Tools is installed" in hint


def test_main_backup_line_uses_the_restore_hint(env, monkeypatch, capsys):
    _, state = env
    state["configured"] = True
    monkeypatch.setattr(setup, "restore_hint", lambda backup: f"RESTORE {backup.name}")
    assert setup.main(["--force", "--yes"]) == 0
    assert "Restore them with: RESTORE objectbuilder-backup.reg" in capsys.readouterr().out


@pytest.mark.parametrize("found, state", [([(9, "Z:\\x\\ObjectBuilder.exe")], (True, 0)), ([], (False, 0)),
                                          (None, (None, 1))])
def test_linux_objectbuilder_state_reads_proc(monkeypatch, found, state):
    seen = {}

    def fake_processes(image):
        seen["image"] = image
        return found

    monkeypatch.setattr(setup.sys, "platform", "linux")
    monkeypatch.setattr(setup.h, "posix_processes", fake_processes)
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: pytest.fail("ran tasklist on Linux"))
    assert setup.objectbuilder_state() == state
    assert seen["image"] == "ObjectBuilder.exe"


def test_linux_main_refuses_while_objectbuilder_runs(env, monkeypatch, capsys):
    _, state = env
    monkeypatch.setattr(setup, "objectbuilder_state", _REAL_OBJECTBUILDER_STATE)
    monkeypatch.setattr(setup.sys, "platform", "linux")
    monkeypatch.setattr(setup.h, "posix_processes", lambda image: [(9, "ObjectBuilder.exe")])
    assert setup.main([]) == 1
    assert "Close Object Builder first" in capsys.readouterr().err
    assert state["imports"] == 0 and state["order"] == []


def test_linux_main_refuses_when_proc_is_unreadable(env, monkeypatch, capsys):
    _, state = env
    monkeypatch.setattr(setup, "objectbuilder_state", _REAL_OBJECTBUILDER_STATE)
    monkeypatch.setattr(setup.sys, "platform", "linux")
    monkeypatch.setattr(setup.h, "posix_processes", lambda image: None)
    assert setup.main([]) == 1
    err = capsys.readouterr().err
    assert "/proc could not be read, so it is unknown whether" in err and "tasklist" not in err
    assert state["imports"] == 0 and state["order"] == []


def test_linux_lenient_running_check_warns_when_proc_is_unreadable(monkeypatch, capsys):
    monkeypatch.setattr(setup.sys, "platform", "linux")
    monkeypatch.setattr(setup.h, "posix_processes", lambda image: None)
    assert setup.objectbuilder_running() is False
    assert "/proc could not be read" in capsys.readouterr().err


def test_linux_import_failure_has_no_administrator_hint(env, monkeypatch, capsys):
    _, state = env
    state["import_rc"] = 1
    monkeypatch.setattr(setup.sys, "platform", "linux")
    assert setup.main([]) == 1
    err = capsys.readouterr().err
    assert "reg import failed (exit 1)" in err and "Administrator" not in err
