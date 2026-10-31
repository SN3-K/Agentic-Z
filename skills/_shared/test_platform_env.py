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
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)  # else a real XDG Steam leaks into the fake home
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


# ------------------------------------------------------------ directory links

def test_make_and_remove_dir_link_never_touch_the_target(tmp_path):
    target = tmp_path / "target"
    (target / "keep").mkdir(parents=True)
    link = tmp_path / "link"
    assert pe.make_dir_link(target, link, junction=True) in ("symlink", "junction")
    assert (link / "keep").is_dir()
    pe.remove_dir_link(link)
    assert not os.path.lexists(link) and (target / "keep").is_dir()


@linux_only
def test_remove_dir_link_removes_a_dangling_link(tmp_path):
    link = tmp_path / "link"
    os.symlink(tmp_path / "gone", link, target_is_directory=True)
    pe.remove_dir_link(link)
    assert not os.path.lexists(link)


@linux_only
def test_fix_hint_commands_on_linux(tmp_path):
    link, folder = tmp_path / "it's a link", tmp_path / "plain"
    os.symlink(tmp_path, link)
    folder.mkdir()
    assert pe.link_cmd("/p/Mods", "/games/DayZ/!Workshop") == "ln -s '/games/DayZ/!Workshop' /p/Mods"
    assert pe.unlink_cmd(link) == f"rm '{tmp_path}/it'\"'\"'s a link'"
    assert pe.unlink_cmd(folder) == f"rmdir {folder}"
    assert pe.rmtree_cmd(folder) == f"rm -rf {folder}"


@windows_only
def test_fix_hint_commands_on_windows():
    assert pe.link_cmd(r"P:\Mods", r"C:\DayZ\!Workshop") == r'cmd /c mklink /J "P:\Mods" "C:\DayZ\!Workshop"'
    assert pe.unlink_cmd(r"P:\Mods") == r'cmd /c rmdir "P:\Mods"'
    assert pe.rmtree_cmd(r"C:\x y") == r'cmd /c rmdir /s /q "C:\x y"'


# ------------------------------------------------------------ processes

@pytest.mark.parametrize("argv0, name", [
    (r"C:\Program Files\DayZ\DayZDiag_x64.exe", "DayZDiag_x64.exe"),
    ("Z:\\home\\me\\DayZ/DayZDiag_x64.exe", "DayZDiag_x64.exe"),
    ("/home/me/.local/share/Steam/ubuntu12_32/steam", "steam"),
    ("steam", "steam"),
])
def test_image_name(argv0, name):
    assert pe.image_name(argv0) == name


def _fake_proc(root: Path, procs: dict) -> Path:
    for pid, argv in procs.items():
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (root / "self").mkdir()  # not a pid
    (root / "999").mkdir()  # exited meanwhile: no cmdline
    return root


def test_posix_processes_matches_the_program_name_in_any_case(tmp_path):
    proc = _fake_proc(tmp_path, {
        12: [r"Z:\games\DayZ\DayZDiag_x64.exe", "-server", "-port=2302"],
        7: [r"C:\DayZ\dayzdiag_x64.exe"],
        30: ["/usr/bin/python3", "proton", "run", r"Z:\games\DayZ\DayZDiag_x64.exe"],  # the launcher, not DayZ
        41: ["/usr/bin/bash"],
    })
    assert pe.posix_processes("DayZDiag_x64.exe", proc) == [
        (7, r"C:\DayZ\dayzdiag_x64.exe"),
        (12, r"Z:\games\DayZ\DayZDiag_x64.exe -server -port=2302"),
    ]


def test_posix_processes_none_when_proc_cannot_be_listed(tmp_path):
    assert pe.posix_processes("x", tmp_path / "missing") is None


def test_kill_posix_processes_skips_processes_that_already_exited(monkeypatch):
    monkeypatch.setattr(pe, "posix_processes", lambda image: [(5, "a"), (6, "b")])

    def kill(pid, sig):
        if pid == 5:
            raise ProcessLookupError
    monkeypatch.setattr(pe.os, "kill", kill)
    assert pe.kill_posix_processes("x") == ([6], [])


def test_detach_kwargs():
    kwargs = pe.detach_kwargs()
    assert kwargs == ({"creationflags": pe.DETACH_FLAGS} if sys.platform == "win32" else {"start_new_session": True})


# ------------------------------------------------------------ host checks

@linux_only
def test_steam_installs_cover_xdg_debian_and_snap_once_each(steam, monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    (xdg / "Steam").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    (tmp_path / ".steam").mkdir()
    (tmp_path / ".steam" / "steam").symlink_to(steam, target_is_directory=True)  # Steam's own link: not a second install
    (tmp_path / ".steam" / "debian-installation").mkdir()
    snap = tmp_path / "snap" / "steam" / "common" / ".local" / "share" / "Steam"
    snap.mkdir(parents=True)
    assert pe.steam_installs() == [
        ("native", xdg / "Steam"), ("native", steam),
        ("native", tmp_path / ".steam" / "debian-installation"), ("snap", snap),
    ]


MOUNTINFO = """\
22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw
40 22 8:17 / /mnt/win rw,relatime shared:2 - ntfs3 /dev/sdb1 rw
41 22 8:33 / /media/usb\\040stick rw shared:3 - vfat /dev/sdc1 rw
42 22 0:50 / /home/me/fuse rw shared:4 - fuse.sshfs host: rw
"""


@pytest.mark.parametrize("path, kind", [
    ("/home/me/dayz-p", "ext4"),
    ("/mnt/win/SteamLibrary", "ntfs3"),
    ("/mnt/window", "ext4"),  # a mount point name prefix is not a parent folder
    ("/media/usb stick/P", "vfat"),
    ("/home/me/fuse/x", "fuse.sshfs"),
])
@linux_only
def test_fs_type_picks_the_longest_containing_mount(tmp_path, path, kind):
    info = tmp_path / "mountinfo"
    info.write_text(MOUNTINFO, encoding="utf-8")
    assert pe.fs_type(path, info) == kind


def test_fs_type_unknown_without_mountinfo(tmp_path):
    assert pe.fs_type("/x", tmp_path / "missing") is None


@linux_only
def test_symlinks_work_probe_leaves_nothing_behind(tmp_path):
    assert pe.symlinks_work(tmp_path) is True
    assert list(tmp_path.iterdir()) == []
    assert pe.symlinks_work(tmp_path / "missing") is None


@linux_only
@pytest.mark.parametrize("grants, writable", [
    ([], False),
    (["xdg-music:ro", "xdg-pictures:ro"], False),  # the Steam Flatpak's defaults
    (["home"], True),
    (["host"], True),
    (["~/dayz-p"], True),
    (["~/dayz-p:ro"], False),
    (["~/dayz-p", "!~/dayz-p"], False),
    (["~/dayz"], False),  # a sibling whose name is a prefix
])
def test_flatpak_can_write(monkeypatch, tmp_path, grants, writable):
    monkeypatch.setattr(pe.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert pe.flatpak_can_write(tmp_path / "dayz-p", grants) is writable


@linux_only
def test_flatpak_grants_read_metadata_and_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(pe.Path, "home", lambda: tmp_path)
    flatpak = tmp_path / ".local" / "share" / "flatpak"
    meta = flatpak / "app" / pe.FLATPAK_STEAM_ID / "current" / "active" / "metadata"
    meta.parent.mkdir(parents=True)
    meta.write_text("[Application]\nname=x\n\n[Context]\nfilesystems=xdg-music:ro;xdg-pictures:ro;\n", encoding="utf-8")
    assert pe._flatpak_filesystems() == ["xdg-music:ro", "xdg-pictures:ro"]
    (flatpak / "overrides").mkdir()
    (flatpak / "overrides" / pe.FLATPAK_STEAM_ID).write_text("[Context]\nfilesystems=/data/dayz-p;\n", encoding="utf-8")
    assert pe._flatpak_filesystems()[-1] == "/data/dayz-p"


@linux_only
def test_snap_can_write_only_visible_home_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(pe.Path, "home", lambda: tmp_path)
    assert pe.snap_can_write(tmp_path / "dayz-p")
    assert not pe.snap_can_write(tmp_path / ".dayz-p")
    assert not pe.snap_can_write("/data/dayz-p")


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A clean Linux host: a P folder on ext4, one native Steam, no Tools prefix. Tests change one piece."""
    p = tmp_path / "P"
    p.mkdir()
    state = {"fs": {}, "installs": [("native", tmp_path / "Steam")], "prefix": None}
    monkeypatch.setattr(pe, "IS_WINDOWS", False)
    monkeypatch.setattr(pe, "IS_LINUX", True)
    monkeypatch.setattr(pe, "posix_processes", lambda image, proc=None: [])
    monkeypatch.setattr(pe, "fs_type", lambda path, mountinfo=None: state["fs"].get(str(path), "ext4"))
    monkeypatch.setattr(pe, "steam_installs", lambda: state["installs"])
    monkeypatch.setattr(pe, "tools_prefix", lambda: state["prefix"])
    monkeypatch.delenv("DAYZ_P_ROOT", raising=False)
    return p, state


def _levels(issues):
    return {i.name: i.level for i in issues}


@linux_only
def test_host_clean(host):
    p, _ = host
    assert set(_levels(pe.host_issues(p, probe_symlinks=True)).values()) == {"ok"}


@linux_only
def test_host_flags_filesystems(host, tmp_path):
    p, state = host
    state["fs"][str(p)] = "vfat"
    assert _levels(pe.host_issues(p))["p_fs"] == "fail"
    state["fs"][str(p)] = "ntfs3"
    game = tmp_path / "DayZ"
    state["fs"][str(game)] = "fuseblk"
    levels = _levels(pe.host_issues(p, game))
    assert levels["p_fs"] == "warn" and levels["game_fs"] == "warn"


@linux_only
def test_host_flags_other_os_missing_steam_and_proc(host, monkeypatch):
    p, state = host
    monkeypatch.setattr(pe, "IS_LINUX", False)
    state["installs"] = []
    monkeypatch.setattr(pe, "posix_processes", lambda image, proc=None: None)
    levels = _levels(pe.host_issues(p))
    assert levels["os"] == levels["steam"] == levels["proc"] == "warn"


@linux_only
def test_host_flags_a_flatpak_steam_that_cannot_write_p(host, monkeypatch, tmp_path):
    p, state = host
    flatpak = tmp_path / "fp" / "Steam"
    state["installs"] = [("flatpak", flatpak)]
    state["prefix"] = flatpak / "steamapps" / "compatdata" / pe.DAYZ_TOOLS_APPID / "pfx"
    (state["prefix"] / "dosdevices").mkdir(parents=True)
    os.symlink(p, state["prefix"] / "dosdevices" / "p:")
    os.symlink("/", state["prefix"] / "dosdevices" / "z:")
    monkeypatch.setattr(pe, "_flatpak_filesystems", lambda: ["xdg-music:ro"])
    [sandbox] = [i for i in pe.host_issues(p) if i.name == "sandbox"]
    assert sandbox.level == "warn" and f"--filesystem={p}" in sandbox.message
    monkeypatch.setattr(pe, "_flatpak_filesystems", lambda: [str(p)])
    assert "sandbox" not in _levels(pe.host_issues(p))


@linux_only
def test_host_flags_prefix_drives(host, monkeypatch, tmp_path):
    p, state = host
    state["prefix"] = tmp_path / "pfx"
    drives = state["prefix"] / "dosdevices"
    drives.mkdir(parents=True)
    os.symlink(tmp_path, drives / "z:")
    levels = _levels(pe.host_issues(p))
    assert levels["prefix_z"] == "warn" and levels["p_mapping"] == "warn"  # z: not /, and no p: at all
    os.symlink(tmp_path / "elsewhere", drives / "p:")
    monkeypatch.setenv("DAYZ_P_ROOT", str(p))
    [mapping] = [i for i in pe.host_issues(p) if i.name == "p_mapping"]
    assert "different folders" in mapping.message


def test_host_issues_empty_on_windows(monkeypatch):
    monkeypatch.setattr(pe, "IS_WINDOWS", True)
    assert pe.host_issues() == []


def test_loads_by_path_without_sys_modules():
    """hooks/scope_guard.py loads this file by path and never registers it; that must work."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("platform_env_by_path", Path(pe.__file__))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.HostIssue("ok", "x", "y").level == "ok"
