"""Where the DayZ work drive and Steam live on this OS (Windows or Linux).

On Windows the work drive is the drive letter P:\\ and Steam is found through
the registry (dayz-preflight does that). On Linux DayZ Tools, DayZ Diag and
DayZ Server run under Steam Proton, and P: is a drive letter inside the DayZ
Tools Proton prefix that points at an ordinary Linux folder. Every skill reads
that folder through p_root(), so the rest of the code only sees a Path.

It also holds the few OS calls the skills share: directory links (a junction
or symlink on Windows, a symlink on Linux), the shell commands quoted in fix
hints, finding and killing processes by image name on Linux, and the Popen
arguments that detach a launched program.

Stdlib only, and it imports nothing else from the repo, so dayz-preflight and
the skills that do not load skill_helpers can import it directly:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
    import platform_env
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from typing import NamedTuple, Optional

IS_WINDOWS = sys.platform == "win32"
# Everything that is not Windows takes the Linux branches; host_issues() warns
# when that is some other OS (macOS, a BSD), which is not supported.
IS_LINUX = sys.platform.startswith("linux")

FLATPAK_STEAM_ID = "com.valvesoftware.Steam"

# Steam app id of DayZ Tools; its Proton prefix is steamapps/compatdata/<id>/.
DAYZ_TOOLS_APPID = "830640"

# p_root() on Linux when no P: folder is set or mapped. /dev/null is a file, so
# this is never a directory and nothing can be created under it: every
# "is P mounted" check fails and no write lands somewhere unexpected.
NO_P_ROOT = Path("/dev/null/P")

# Popen creationflags for a program that must outlive this script and the
# console it ran in: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP.
DETACH_FLAGS = 0x00000008 | 0x00000200


def _linux_steam_candidates() -> list[tuple[str, Path]]:
    """(kind, folder) for every place a Linux Steam install can live; kind is native, flatpak or snap."""
    home = Path.home()
    data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return [
        ("native", data_home / "Steam"),
        ("native", home / ".local" / "share" / "Steam"),
        ("native", home / ".steam" / "steam"),  # Steam's own link to the install, on every distro
        ("native", home / ".steam" / "root"),
        ("native", home / ".steam" / "debian-installation"),  # Debian and Ubuntu's steam package
        ("flatpak", home / ".var" / "app" / FLATPAK_STEAM_ID / ".local" / "share" / "Steam"),
        ("snap", home / "snap" / "steam" / "common" / ".local" / "share" / "Steam"),
    ]


def steam_installs() -> list[tuple[str, Path]]:
    """(kind, folder) of each Linux Steam install that exists, one entry per real folder."""
    found, seen = [], set()
    for kind, root in _linux_steam_candidates():
        if not root.is_dir():
            continue
        key = os.path.realpath(root)
        if key not in seen:
            seen.add(key)
            found.append((kind, root))
    return found


def linux_steam_roots() -> list[Path]:
    """The Linux Steam install folders that exist: native first, then Flatpak and Snap.

    ~/.steam/steam and ~/.steam/root are usually links to the real install;
    each real folder is listed once.
    """
    return [root for _, root in steam_installs()]


def parse_library_folders(text: str) -> list[Path]:
    """Library roots listed in a Steam steamapps/libraryfolders.vdf file.

    VDF escapes backslashes, so "D:\\\\SteamLibrary" in the file is D:\\SteamLibrary.
    On Linux the paths are plain POSIX paths and come back unchanged.
    """
    return [
        Path(m.group(1).replace("\\\\", "\\"))
        for m in re.finditer(r'"path"\s+"([^"]+)"', text)
    ]


def steam_libraries(steam_roots: list[Path]) -> list[Path]:
    """Every Steam library root: each root itself plus its libraryfolders.vdf entries.

    Duplicates (the same folder reached through a link, or listed twice) are dropped.
    """
    libraries: list[Path] = []
    seen: set[str] = set()

    def add(lib: Path) -> None:
        key = os.path.normcase(os.path.realpath(lib))
        if key not in seen:
            seen.add(key)
            libraries.append(lib)

    for root in steam_roots:
        add(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                text = vdf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lib in parse_library_folders(text):
                add(lib)
    return libraries


def tools_prefix() -> Optional[Path]:
    """The DayZ Tools Proton prefix (compatdata/830640/pfx) on Linux, or None."""
    if IS_WINDOWS:
        return None
    for lib in steam_libraries(linux_steam_roots()):
        pfx = lib / "steamapps" / "compatdata" / DAYZ_TOOLS_APPID / "pfx"
        if pfx.is_dir():
            return pfx
    return None


def p_root() -> Path:
    """The folder that is the DayZ work drive P:.

    Windows: P:\\ itself.
    Linux, first hit wins:
      1. $DAYZ_P_ROOT
      2. the target of pfx/dosdevices/p: in the DayZ Tools Proton prefix
      3. NO_P_ROOT, which never exists
    The result is not checked for existence; callers test .is_dir() as they do for P:\\.
    """
    if IS_WINDOWS:
        return Path("P:\\")
    env = os.environ.get("DAYZ_P_ROOT")
    if env:
        return Path(env).expanduser()
    pfx = tools_prefix()
    if pfx is not None:
        link = pfx / "dosdevices" / "p:"
        if os.path.lexists(link):
            return Path(os.path.realpath(link))
    return NO_P_ROOT


def p_root_hint() -> str:
    """One line telling the user how to make P: available on this OS."""
    if IS_WINDOWS:
        return ("Open DayZ Tools and mount the P drive "
                "(Tools menu > Mount P drive), or run /dayz-workdrive.")
    return ("Set DAYZ_P_ROOT to your work drive folder, or map it as P: in the DayZ Tools "
            f"Proton prefix (steamapps/compatdata/{DAYZ_TOOLS_APPID}/pfx/dosdevices/p:).")


def child_ci(parent: Path, name: str) -> Path:
    """parent/name, matching name case-insensitively when the exact case is missing.

    Windows paths ignore case, so code written there mixes P:\\dz and P:\\DZ.
    Linux does not, and Extract Game Data writes DZ. Returns parent/name
    unchanged when nothing matches, so callers still get a sensible missing path.
    """
    exact = parent / name
    if IS_WINDOWS or os.path.lexists(exact):
        return exact
    try:
        for entry in parent.iterdir():
            if entry.name.lower() == name.lower():
                return entry
    except OSError:
        pass
    return exact


def local_p_path(path: str, root: Optional[Path] = None) -> Optional[Path]:
    """A path on P: as a local Path, or None when it is not on P:.

    Takes the P:\\... strings the search index stores (built on Windows), and on
    Linux also a plain path under the P root. `..` is collapsed lexically first,
    so P:\\..\\x stays P:\\x and never leaves the drive. On Linux each part is
    matched case-insensitively (the index says P:\\dz, the folder is DZ).
    """
    if IS_WINDOWS:
        p = Path(os.path.normpath(path))
        return p if p.drive.upper() == "P:" else None
    root = p_root() if root is None else root
    win = PureWindowsPath(path)
    if win.drive.upper() == "P:":
        if not win.root:
            return None  # P:foo is relative to P:'s current folder; nothing to map it to
        # "/" + the parts after P:\, so normpath drops a leading ".." at the root as Windows does.
        parts = Path(posixpath.normpath("/" + "/".join(win.parts[1:]))).parts[1:]
    else:
        p = Path(path)
        if not p.is_absolute():
            return None
        try:
            parts = Path(os.path.normpath(p)).relative_to(Path(os.path.normpath(root))).parts
        except ValueError:
            return None
    local = root
    for part in parts:
        local = child_ci(local, part)
    return local


def to_wine_path(path: Path, root: Optional[Path] = None) -> str:
    """A path as a Windows program under Proton sees it.

    A path under the P root becomes P:\\...; any other absolute path becomes
    Z:\\..., Wine's drive for /. On Windows the path is returned as it is.
    """
    if IS_WINDOWS:
        return str(path)
    root = p_root() if root is None else root
    full = Path(os.path.abspath(path))
    try:
        rel = full.relative_to(Path(os.path.abspath(root)))
    except ValueError:
        return str(PureWindowsPath("Z:\\", *full.parts[1:]))
    return str(PureWindowsPath("P:\\", *rel.parts))


# ------------------------------------------------------------ directory links

def make_dir_link(target: Path, link: Path, junction: bool = False) -> str:
    """Create `link` pointing at the folder `target`; returns "symlink" or "junction".

    Windows: a directory symlink, falling back to a junction (mklink /J needs no
    admin rights) when symlinks need elevation; junction=True goes straight to
    the junction. Linux: a symlink. Raises OSError with mklink's own message.
    """
    if IS_WINDOWS:
        if not junction:
            try:
                os.symlink(target, link, target_is_directory=True)
                return "symlink"
            except OSError:
                pass
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise OSError(detail or f"mklink exited {result.returncode}")
        return "junction"
    os.symlink(target, link, target_is_directory=True)
    return "symlink"


def remove_dir_link(path: Path) -> None:
    """Remove a junction or directory symlink, never its target.

    Windows: rmdir on a reparse point deletes only the link, even when it
    dangles or its target is not empty. Linux: unlink (rmdir refuses a symlink).
    """
    if IS_WINDOWS:
        os.rmdir(path)
    else:
        os.unlink(path)


def _quote(path) -> str:
    return f'"{path}"' if IS_WINDOWS else shlex.quote(str(path))


def link_cmd(link, target) -> str:
    """The shell command a user runs to create a directory link, for fix hints."""
    if IS_WINDOWS:
        return f"cmd /c mklink /J {_quote(link)} {_quote(target)}"
    return f"ln -s {_quote(target)} {_quote(link)}"


def unlink_cmd(path) -> str:
    """The shell command that removes a directory link (or an empty folder) only."""
    if IS_WINDOWS:
        return f"cmd /c rmdir {_quote(path)}"
    return f"rm {_quote(path)}" if os.path.islink(path) else f"rmdir {_quote(path)}"


def rmtree_cmd(path) -> str:
    """The shell command that deletes a folder and everything in it."""
    if IS_WINDOWS:
        return f"cmd /c rmdir /s /q {_quote(path)}"
    return f"rm -rf {_quote(path)}"


# ------------------------------------------------------------ processes (Linux)

def image_name(argv0: str) -> str:
    """The program name in argv[0]: the part after the last / or \\.

    Wine shows a Windows program's argv[0] as its Windows path
    (C:\\...\\DayZDiag_x64.exe), so both separators count.
    """
    return re.split(r"[\\/]", argv0)[-1]


def posix_processes(image: str, proc: Path = Path("/proc")) -> Optional[list[tuple[int, str]]]:
    """(pid, command line) of every process whose image name is `image` (any case).

    Reads /proc/<pid>/cmdline, so it also finds Windows programs running under
    Proton/Wine, whose /proc comm is cut to 15 characters. Processes that exit
    or cannot be read meanwhile are skipped. None when /proc cannot be listed.
    """
    try:
        entries = [e for e in os.listdir(proc) if e.isdigit()]
    except OSError:
        return None
    found = []
    for entry in entries:
        try:
            raw = (proc / entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = raw.decode("utf-8", errors="replace").split("\0")
        if argv and argv[-1] == "":
            argv.pop()
        if argv and image_name(argv[0]).lower() == image.lower():
            found.append((int(entry), " ".join(argv)))
    return sorted(found)


def kill_posix_processes(image: str) -> tuple[list[int], list[str]]:
    """SIGKILL every process named `image` (taskkill /F). Returns (killed pids, errors)."""
    killed, errors = [], []
    for pid, _ in posix_processes(image) or []:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue  # exited meanwhile: already the wanted end state
        except OSError as e:
            errors.append(f"PID {pid}: {e.strerror or e}")
            continue
        killed.append(pid)
    return killed, errors


def detach_kwargs() -> dict:
    """Popen arguments for a program that must outlive this script and its console.

    Windows: DETACH_FLAGS. Linux: a new session, so closing the shell does not
    send it SIGHUP.
    """
    return {"creationflags": DETACH_FLAGS} if IS_WINDOWS else {"start_new_session": True}


# ------------------------------------------------------------ host checks (Linux)

# Filesystems that cannot hold symlinks: P:\<Mod> and P:\Mods are symlinks on Linux.
NO_SYMLINK_FS = {"vfat", "msdos", "exfat"}
# NTFS from a Windows dual boot: symlinks work, but Proton and Wine prefixes on it misbehave
# (no Unix permissions, case-insensitive lookups, lost files after Windows Fast Startup).
NTFS_FS = {"ntfs", "ntfs3", "fuseblk"}


class HostIssue(NamedTuple):
    """A NamedTuple, not a dataclass: hooks load this file by path without sys.modules, which a dataclass needs."""
    level: str  # "ok", "warn" or "fail"
    name: str
    message: str


def _unescape_mount(field: str) -> str:
    """A mountinfo path field: spaces, tabs, newlines and backslashes are octal escapes."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def fs_type(path, mountinfo: Path = Path("/proc/self/mountinfo")) -> Optional[str]:
    """The filesystem type (ext4, btrfs, ntfs3, vfat, ...) holding path, or None if unknown.

    The longest mount point that contains the resolved path wins. A FUSE mount
    reports its subtype as fuse.<name>, and NTFS through ntfs-3g shows as fuseblk.
    """
    try:
        lines = mountinfo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    target = os.path.realpath(path)
    best, best_type = "", None
    for line in lines:
        fields = line.split()
        if "-" not in fields:
            continue
        sep = fields.index("-")
        if len(fields) < 5 or sep + 1 >= len(fields):
            continue
        mount = _unescape_mount(fields[4])
        inside = target == mount or target.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) >= len(best):
            best, best_type = mount, fields[sep + 1]
    return best_type


def symlinks_work(folder: Path) -> Optional[bool]:
    """True when a symlink can be made in folder (made and removed at once), None if folder is missing.

    Writes a uniquely named link, so it never touches an existing file.
    """
    if not folder.is_dir():
        return None
    probe = Path(tempfile.mktemp(prefix=".agentic-z-linkprobe-", dir=folder))
    try:
        os.symlink(".", probe, target_is_directory=True)
    except OSError:
        return False
    try:
        os.unlink(probe)
    except OSError:
        pass
    return True


def _flatpak_filesystems(app_id: str = FLATPAK_STEAM_ID) -> Optional[list[str]]:
    """The filesystems= grants of a Flatpak app (its metadata, then system and user overrides), or None if not installed."""
    home = Path.home()
    metadata = [
        home / ".local" / "share" / "flatpak" / "app" / app_id / "current" / "active" / "metadata",
        Path("/var/lib/flatpak/app") / app_id / "current" / "active" / "metadata",
    ]
    overrides = [
        Path("/var/lib/flatpak/overrides/global"),
        Path("/var/lib/flatpak/overrides") / app_id,
        home / ".local" / "share" / "flatpak" / "overrides" / "global",
        home / ".local" / "share" / "flatpak" / "overrides" / app_id,
    ]
    meta = next((m for m in metadata if m.is_file()), None)
    if meta is None:
        return None
    grants: list[str] = []
    for f in [meta, *overrides]:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        section = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line
            elif section == "[Context]" and line.startswith("filesystems="):
                grants += [g for g in line.split("=", 1)[1].split(";") if g]
    return grants


def flatpak_can_write(path, grants: list[str]) -> bool:
    """True when the filesystems= grants give a Flatpak app write access to path.

    Grants apply in order and a !grant takes one back. host and home cover what
    their names say; ~/x and /x cover that folder. A :ro grant is not enough:
    the tools write their output under P:.
    """
    home = os.path.realpath(Path.home())
    target = os.path.realpath(path)
    allowed = False
    for grant in grants:
        negate = grant.startswith("!")
        name, _, mode = grant.lstrip("!").partition(":")
        if name in ("host", "host-os"):
            covers = name == "host"
        elif name == "home":
            covers = target == home or target.startswith(home + "/")
        elif name.startswith(("~", "/")):
            root = os.path.realpath(os.path.expanduser(name))
            covers = target == root or target.startswith(root.rstrip("/") + "/")
        else:
            continue  # xdg-music, xdg-download, ...: not where a work drive lives
        if covers:
            allowed = not negate and mode != "ro"
    return allowed


def snap_can_write(path) -> bool:
    """True when the Steam snap's home interface reaches path: under $HOME, with no hidden folder on the way."""
    home = os.path.realpath(Path.home())
    target = os.path.realpath(path)
    if not target.startswith(home + "/"):
        return False
    return not any(part.startswith(".") for part in Path(target).relative_to(home).parts)


def _tools_install_kind(prefix: Optional[Path]) -> Optional[str]:
    """native, flatpak or snap: the Steam install the DayZ Tools prefix belongs to."""
    if prefix is None:
        return None
    real = os.path.realpath(prefix)
    for kind, root in steam_installs():
        r = os.path.realpath(root)
        if real.startswith(r.rstrip("/") + "/"):
            return kind
    return "native"  # a library on another drive: its Steam is the native one unless a sandbox owns it


def host_issues(p: Optional[Path] = None, game_root: Optional[Path] = None,
                probe_symlinks: bool = False) -> list[HostIssue]:
    """Checks of this Linux host that the skills depend on; [] on Windows.

    p is the P folder (default p_root()), game_root the DayZ install, when known.
    probe_symlinks=True makes and removes one symlink in the P folder; without
    it every check is read-only.
    """
    if IS_WINDOWS:
        return []
    issues: list[HostIssue] = []
    add = lambda level, name, message: issues.append(HostIssue(level, name, message))  # noqa: E731

    if IS_LINUX:
        add("ok", "os", f"Linux ({sys.platform})")
    else:
        add("warn", "os", f"{sys.platform} is not supported: only Windows and Linux are. "
                          "The Linux code paths are used, untested.")

    # hidepid hides other users' processes only; DayZDiag runs as this user, so it stays visible.
    if posix_processes("") is None or not os.path.isfile("/proc/self/cmdline"):
        add("warn", "proc", "/proc cannot be read, so /dayz-stop-test, the gate and the status line "
                            "cannot see DayZDiag. Check that /proc is mounted.")
    else:
        add("ok", "proc", "/proc readable")

    installs = steam_installs()
    if not installs:
        add("warn", "steam", "no Steam install found in ~/.local/share/Steam, ~/.steam, the Flatpak or the Snap. "
                             "Set DAYZ_TOOLS_PATH and DAYZ_GAME_PATH if Steam lives elsewhere.")
    else:
        add("ok", "steam", "Steam: " + ", ".join(f"{kind} at {root}" for kind, root in installs))

    p = p_root() if p is None else p
    p_ok = p != NO_P_ROOT and p.is_dir()
    prefix = tools_prefix()

    if p_ok:
        kind = fs_type(p)
        if kind in NO_SYMLINK_FS:
            add("fail", "p_fs", f"{p} is on {kind}, which cannot hold symlinks; P:\\<Mod> and P:\\Mods are "
                                "symlinks on Linux. Move the work drive folder to an ext4, btrfs or xfs drive.")
        elif kind in NTFS_FS:
            add("warn", "p_fs", f"{p} is on NTFS ({kind}). Proton tools on NTFS are unreliable; a Linux "
                                "filesystem (ext4, btrfs, xfs) is safer. Disable Windows Fast Startup if you keep it.")
        elif kind:
            add("ok", "p_fs", f"{p} is on {kind}")
        if probe_symlinks:
            works = symlinks_work(p)
            if works is False:
                add("fail", "p_symlink", f"a symlink cannot be created in {p}, so /dayz-new-mod, /dayz-import-mod "
                                         "and the P:\\Mods link will fail. Check the folder's filesystem and permissions.")
            elif works:
                add("ok", "p_symlink", f"symlinks can be created in {p}")

        install = _tools_install_kind(prefix)
        if install == "flatpak":
            grants = _flatpak_filesystems()
            if grants is not None and not flatpak_can_write(p, grants):
                add("warn", "sandbox", f"DayZ Tools run in the Steam Flatpak, which cannot write {p}. Grant it: "
                                       f"flatpak override --user --filesystem={p} {FLATPAK_STEAM_ID}")
        elif install == "snap" and not snap_can_write(p):
            add("warn", "sandbox", f"DayZ Tools run in the Steam Snap, which only reaches non-hidden folders "
                                   f"in your home; {p} is outside that. Move the work drive folder under ~/.")

    if game_root is not None and fs_type(game_root) in NTFS_FS:
        add("warn", "game_fs", f"DayZ is installed on NTFS ({game_root}). Proton games on NTFS are unreliable; "
                               "a Steam library on a Linux filesystem is safer.")

    if prefix is not None:
        z = prefix / "dosdevices" / "z:"
        if os.path.lexists(z) and os.path.realpath(z) != "/":
            add("warn", "prefix_z", f"{z} points to {os.path.realpath(z)}, not /. Paths outside P: are passed to "
                                    "the tools as Z:\\..., which then name the wrong files. Point z: back at /.")
        p_link = prefix / "dosdevices" / "p:"
        mapped = Path(os.path.realpath(p_link)) if os.path.lexists(p_link) else None
        if os.environ.get("DAYZ_P_ROOT") and mapped is not None and os.path.realpath(p) != str(mapped):
            add("warn", "p_mapping", f"DAYZ_P_ROOT is {p}, but DayZ Tools see P: as {mapped} "
                                     f"({p_link}). The skills and the tools would use different folders.")
        elif mapped is None and not os.environ.get("DAYZ_P_ROOT"):
            add("warn", "p_mapping", f"the DayZ Tools prefix ({prefix}) has no p: drive. Map it: "
                                     f"ln -s <work drive folder> {shlex.quote(str(p_link))}")
    return issues
