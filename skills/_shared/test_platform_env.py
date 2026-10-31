"""Tests for _shared/platform_env.py. Linux-only branches are skipped on Windows and the reverse."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import platform_env as pe  # noqa: E402

linux_only = pytest.mark.skipif(sys.platform == "win32", reason="P: is a folder only on Linux")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="P: is a drive letter only on Windows")


@pytest.fixture
def steam(monkeypatch, tmp_path):
    """A fake home with a native Steam install; returns its root."""
    monkeypatch.setattr(pe.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("DAYZ_P_ROOT", raising=False)
    root = tmp_path / ".local" / "share" / "Steam"
    (root / "steamapps").mkdir(parents=True)
    return root


def _tools_prefix(lib: Path) -> Path:
    pfx = lib / "steamapps" / "compatdata" / pe.DAYZ_TOOLS_APPID / "pfx"
    (pfx / "dosdevices").mkdir(parents=True)
    return pfx


def test_parse_library_folders_keeps_posix_paths():
    vdf = '"libraryfolders"\n{\n "0"\n {\n  "path"  "/home/me/.local/share/Steam"\n }\n}\n'
    assert pe.parse_library_folders(vdf) == [Path("/home/me/.local/share/Steam")]


def test_steam_libraries_adds_vdf_entries_once(tmp_path):
    root, extra = tmp_path / "Steam", tmp_path / "Games"
    (root / "steamapps").mkdir(parents=True)
    extra.mkdir()
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        f'"path" "{root.as_posix()}"\n"path" "{extra.as_posix()}"\n', encoding="utf-8")
    assert pe.steam_libraries([root, root]) == [root, Path(extra.as_posix())]


@linux_only
def test_linux_steam_roots_finds_native_and_flatpak(steam, tmp_path):
    flatpak = tmp_path / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    flatpak.mkdir(parents=True)
    assert pe.linux_steam_roots() == [steam, flatpak]


@linux_only
def test_p_root_prefers_dayz_p_root(steam, monkeypatch, tmp_path):
    os.symlink(tmp_path, _tools_prefix(steam) / "dosdevices" / "p:")
    monkeypatch.setenv("DAYZ_P_ROOT", str(tmp_path / "work"))
    assert pe.p_root() == tmp_path / "work"


@linux_only
def test_p_root_reads_the_tools_prefix_p_drive(steam, tmp_path):
    work = tmp_path / "dayz-p"
    work.mkdir()
    os.symlink(work, _tools_prefix(steam) / "dosdevices" / "p:")
    assert pe.p_root() == work


@linux_only
def test_p_root_finds_the_prefix_in_another_library(steam, tmp_path):
    lib = tmp_path / "games"
    work = tmp_path / "dayz-p"
    work.mkdir()
    os.symlink(work, _tools_prefix(lib) / "dosdevices" / "p:")
    (steam / "steamapps" / "libraryfolders.vdf").write_text(f'"path" "{lib}"\n', encoding="utf-8")
    assert pe.p_root() == work


@linux_only
def test_p_root_without_a_mapping_never_exists(steam):
    _tools_prefix(steam)  # a prefix with no p: drive
    assert pe.p_root() == pe.NO_P_ROOT
    assert not pe.NO_P_ROOT.is_dir()
    with pytest.raises(OSError):
        (pe.NO_P_ROOT / "Mods").mkdir(parents=True)


@windows_only
def test_p_root_on_windows_is_the_drive_letter():
    assert pe.p_root() == Path("P:\\")


@linux_only
def test_child_ci_matches_other_case(tmp_path):
    (tmp_path / "DZ").mkdir()
    assert pe.child_ci(tmp_path, "dz") == tmp_path / "DZ"
    assert pe.child_ci(tmp_path, "DZ") == tmp_path / "DZ"
    assert pe.child_ci(tmp_path, "gone") == tmp_path / "gone"
    assert pe.child_ci(tmp_path / "missing", "x") == tmp_path / "missing" / "x"


@linux_only
@pytest.mark.parametrize("given, expected", [
    (r"P:\dz\data\config.cpp", ("DZ", "data", "config.cpp")),
    ("p:/scripts/a.c", ("scripts", "a.c")),
    (r"P:\..\..\etc\passwd", ("etc", "passwd")),  # `..` stops at the drive root, as on Windows
    (r"P:\dz\..\scripts\a.c", ("scripts", "a.c")),
])
def test_local_p_path_maps_p_paths_under_the_root(tmp_path, given, expected):
    (tmp_path / "DZ" / "data").mkdir(parents=True)
    assert pe.local_p_path(given, tmp_path) == tmp_path.joinpath(*expected)


@linux_only
def test_local_p_path_accepts_paths_under_the_root_only(tmp_path):
    assert pe.local_p_path(str(tmp_path / "a" / ".." / "b.c"), tmp_path) == tmp_path / "b.c"
    for outside in (str(tmp_path / ".." / "x"), "/etc/passwd", "relative/x.c", r"C:\Windows\win.ini", "P:x.c"):
        assert pe.local_p_path(outside, tmp_path) is None, outside


@linux_only
def test_to_wine_path_uses_p_under_the_root_and_z_elsewhere(tmp_path):
    assert pe.to_wine_path(tmp_path / "DZ" / "a.c", tmp_path) == "P:\\DZ\\a.c"
    assert pe.to_wine_path(Path("/opt/x y/b.exe"), tmp_path) == "Z:\\opt\\x y\\b.exe"


@windows_only
def test_to_wine_path_is_unchanged_on_windows():
    assert pe.to_wine_path(Path(r"C:\x\y.c")) == r"C:\x\y.c"
