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
from pathlib import Path, PureWindowsPath
from typing import Optional

IS_WINDOWS = sys.platform == "win32"

# Steam app id of DayZ Tools; its Proton prefix is steamapps/compatdata/<id>/.
DAYZ_TOOLS_APPID = "830640"

# p_root() on Linux when no P: folder is set or mapped. /dev/null is a file, so
# this is never a directory and nothing can be created under it: every
# "is P mounted" check fails and no write lands somewhere unexpected.
NO_P_ROOT = Path("/dev/null/P")

# Popen creationflags for a program that must outlive this script and the
# console it ran in: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP.
DETACH_FLAGS = 0x00000008 | 0x00000200


def linux_steam_roots() -> list[Path]:
    """The Linux Steam install folders that exist, native first, then Flatpak.

    ~/.steam/steam is usually a link to ~/.local/share/Steam; both are kept,
    and callers drop duplicates by resolved path.
    """
    home = Path.home()
    candidates = [
        home / ".local" / "share" / "Steam",
        home / ".steam" / "steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    ]
    return [root for root in candidates if root.is_dir()]


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
