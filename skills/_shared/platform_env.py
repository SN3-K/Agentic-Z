"""Where the DayZ work drive and Steam live on this OS (Windows or Linux).

On Windows the work drive is the drive letter P:\\ and Steam is found through
the registry (dayz-preflight does that). On Linux DayZ Tools, DayZ Diag and
DayZ Server run under Steam Proton, and P: is a drive letter inside the DayZ
Tools Proton prefix that points at an ordinary Linux folder. Every skill reads
that folder through p_root(), so the rest of the code only sees a Path.

It also holds the few OS calls the skills share: directory links (a junction
or symlink on Windows, a symlink on Linux), the shell commands quoted in fix
hints, finding and killing processes by image name on Linux, the Popen
arguments that detach a launched program, and running the Windows DayZ Tools
programs (directly on Windows, through Steam Proton on Linux).

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


# ------------------------------------------------------------ Windows tools (Proton on Linux)

# Proton folder to use instead of the one Steam picks for DayZ Tools.
PROTON_ENV = "DAYZ_PROTON"


class ProtonError(RuntimeError):
    """Proton, the Steam Linux Runtime it needs, or the DayZ Tools prefix cannot be found,
    or a program did not start under Proton.

    Most messages are one line that ends with the fix; a failed start ends with
    Proton's last output lines instead.
    """


def parse_vdf(text: str) -> dict:
    """A Steam VDF/ACF text file as nested dicts of strings.

    Enough for config.vdf, toolmanifest.vdf, compatibilitytool.vdf and
    appmanifest_*.acf: quoted keys and values, and { } blocks.
    """
    root: dict = {}
    stack = [root]
    key = None
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        if m.group(2) == "{":
            child: dict = {}
            stack[-1][key if key is not None else ""] = child
            stack.append(child)
            key = None
        elif m.group(2) == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = m.group(1).replace("\\\\", "\\")
        else:
            stack[-1][key] = m.group(1).replace("\\\\", "\\")
            key = None
    return root


def _read_vdf(path: Path) -> dict:
    """parse_vdf of a file; {} when it is missing or unreadable."""
    try:
        return parse_vdf(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def _vdf_find(tree: dict, name: str):
    """The value of the first key called name (any case) anywhere in tree, or None."""
    for key, value in tree.items():
        if key.lower() == name.lower():
            return value
        if isinstance(value, dict):
            found = _vdf_find(value, name)
            if found is not None:
                return found
    return None


def _sub_dirs(folder: Path) -> list[Path]:
    """The folders inside folder, sorted; [] when it cannot be listed."""
    try:
        return sorted(entry for entry in folder.iterdir() if entry.is_dir())
    except OSError:
        return []


def _is_proton(folder: Path) -> bool:
    return (folder / "proton").is_file()


def _compat_tool_names(steam_roots: list[Path]) -> list[str]:
    """The compatibility tool names config.vdf sets for DayZ Tools: its own mapping first, then the default ("0")."""
    mappings = [_vdf_find(_read_vdf(root / "config" / "config.vdf"), "CompatToolMapping") for root in steam_roots]
    names = []
    for appid in (DAYZ_TOOLS_APPID, "0"):
        for mapping in mappings:
            entry = mapping.get(appid) if isinstance(mapping, dict) else None
            if isinstance(entry, dict) and entry.get("name"):
                names.append(entry["name"])
    return names


def _custom_tools(steam_roots: list[Path]) -> dict[str, Path]:
    """Internal name -> folder of each tool in <steam root>/compatibilitytools.d (GE-Proton and the like)."""
    tools: dict[str, Path] = {}
    for root in steam_roots:
        for folder in _sub_dirs(root / "compatibilitytools.d"):
            compat_tools = _vdf_find(_read_vdf(folder / "compatibilitytool.vdf"), "compat_tools")
            if not isinstance(compat_tools, dict):
                continue
            for name, info in compat_tools.items():
                install_path = info.get("install_path", ".") if isinstance(info, dict) else "."
                tools.setdefault(name, folder / install_path)
    return tools


def _official_folder_names(name: str) -> list[str]:
    """The steamapps/common folder names Valve's Proton with this internal name can have.

    proton_9 is "Proton 9.0", proton_63 is "Proton 6.3"; a folder may add a
    suffix such as " (Beta)", which the caller allows.
    """
    if name == "proton_experimental":
        return ["Proton - Experimental"]
    if name == "proton_hotfix":
        return ["Proton Hotfix"]
    m = re.fullmatch(r"proton_(\d+)", name)
    if not m:
        return []
    digits = m.group(1)
    names = [f"Proton {digits}.0"]
    if len(digits) > 1:
        names.append(f"Proton {digits[0]}.{digits[1:]}")
    return names


def _proton_by_name(name: str, steam_roots: list[Path], libraries: list[Path]) -> Optional[Path]:
    """The installed Proton folder for a compatibility tool name, or None."""
    custom = _custom_tools(steam_roots).get(name)
    if custom is not None and _is_proton(custom):
        return custom
    for base in _official_folder_names(name):
        for lib in libraries:
            common = lib / "steamapps" / "common"
            if _is_proton(common / base):
                return common / base
            for folder in _sub_dirs(common):
                if folder.name.startswith(base + " ") and _is_proton(folder):
                    return folder
    return None


def _major_minor(text: str) -> Optional[str]:
    """"9.0" from a version such as "9.0-203" or "1749140930 proton-9.0-4f"."""
    m = re.search(r"(\d+)\.(\d+)", text)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def _read_version(path: Path) -> Optional[str]:
    try:
        return _major_minor(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _proton_matching_prefix(steam_roots: list[Path], libraries: list[Path]) -> Optional[Path]:
    """The installed Proton with the major.minor version that last ran the DayZ Tools prefix, or None."""
    wanted = None
    for lib in libraries:
        wanted = _read_version(lib / "steamapps" / "compatdata" / DAYZ_TOOLS_APPID / "version")
        if wanted:
            break
    if not wanted:
        return None
    candidates = [folder for lib in libraries for folder in _sub_dirs(lib / "steamapps" / "common")
                  if folder.name.startswith("Proton")]
    candidates += list(_custom_tools(steam_roots).values())
    for folder in candidates:
        if _is_proton(folder) and _read_version(folder / "version") == wanted:
            return folder
    return None


def find_proton(steam_root: Optional[Path] = None) -> Optional[Path]:
    """The Proton folder that runs the DayZ Tools programs on Linux, or None (always None on Windows).

    First hit wins:
      1. $DAYZ_PROTON (None when it is set but holds no proton file)
      2. the compatibility tool Steam's config.vdf sets for DayZ Tools (830640)
      3. the default compatibility tool ("0")
      4. the installed Proton whose version last ran the DayZ Tools prefix
    steam_root limits the search to one Steam install; default: every one found.
    """
    if IS_WINDOWS:
        return None
    override = os.environ.get(PROTON_ENV)
    if override:
        folder = Path(override).expanduser()
        return folder if _is_proton(folder) else None
    roots = [steam_root] if steam_root is not None else linux_steam_roots()
    libraries = steam_libraries(roots)
    for name in _compat_tool_names(roots):
        folder = _proton_by_name(name, roots, libraries)
        if folder is not None:
            return folder
    return _proton_matching_prefix(roots, libraries)


def required_runtime_appid(proton_dir: Path) -> Optional[str]:
    """The Steam app id of the Linux Runtime a Proton runs in (its toolmanifest.vdf), or None for old builds without one."""
    appid = _vdf_find(_read_vdf(proton_dir / "toolmanifest.vdf"), "require_tool_appid")
    return appid if isinstance(appid, str) and appid else None


def find_steam_runtime(proton_dir: Path) -> Optional[Path]:
    """The _v2-entry-point of the Steam Linux Runtime proton_dir needs.

    None when the Proton needs no runtime (required_runtime_appid is None) or
    the runtime is not installed in any Steam library; tell them apart with
    required_runtime_appid.
    """
    appid = required_runtime_appid(proton_dir)
    if appid is None:
        return None
    for lib in steam_libraries(linux_steam_roots()):
        installdir = _vdf_find(_read_vdf(lib / "steamapps" / f"appmanifest_{appid}.acf"), "installdir")
        if isinstance(installdir, str) and installdir:
            entry = lib / "steamapps" / "common" / installdir / "_v2-entry-point"
            if entry.is_file():
                return entry
    return None


def _proton_missing_message() -> str:
    override = os.environ.get(PROTON_ENV)
    if override:
        return (f"{PROTON_ENV} is {override}, which has no proton file. "
                f"Point {PROTON_ENV} at a Proton folder (the one holding the proton script).")
    return ("no Proton found for DayZ Tools. In Steam, open DayZ Tools > Properties > Compatibility and "
            f"force a Proton version, or set {PROTON_ENV} to a Proton folder.")


def _runtime_missing_message(proton_dir: Path, appid: str) -> str:
    return (f"{proton_dir.name} needs the Steam Linux Runtime (app {appid}), which is not installed. "
            "Install it from the Steam library (Tools), or start DayZ Tools once from Steam.")


def windows_tool_cmd(exe, args: list[str]) -> tuple[list[str], dict]:
    """(argv, extra environment) that run the Windows program exe with args.

    Windows: the program itself and no extra environment. Linux: the Steam
    Linux Runtime's entry point running Proton's run verb on exe, in the
    DayZ Tools prefix, so P: is mapped. The run verb, not waitforexitandrun:
    waitforexitandrun first waits for every process in the prefix to exit, and
    the prefix is shared by all DayZ Tools, so an open Workbench or Object
    Builder would block every build, pack and registry call until it closed. args go through unchanged: callers
    turn path arguments into P:\\ or Z:\\ paths with to_wine_path. They are
    passed as argv, not through cmd.exe, so % and cmd operators in them are
    taken literally; the % and quote limits of run_windows_tool do not apply
    here. Raises ProtonError when Proton, its runtime or the prefix cannot be found.
    """
    if IS_WINDOWS:
        return [str(exe), *args], {}
    proton = find_proton()
    if proton is None:
        raise ProtonError(_proton_missing_message())
    prefix = tools_prefix()
    if prefix is None:
        raise ProtonError(f"the DayZ Tools Proton prefix (steamapps/compatdata/{DAYZ_TOOLS_APPID}) is missing. "
                          "Install DayZ Tools in Steam and start it once.")
    env = {
        # tools_prefix() found the prefix in a Steam library, so a Steam install exists.
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(linux_steam_roots()[0]),
        "STEAM_COMPAT_DATA_PATH": str(prefix.parent),
    }
    # Both the proton script and _v2-entry-point take exe as a Linux path.
    command = [str(proton / "proton"), "run", str(exe), *args]
    appid = required_runtime_appid(proton)
    if appid is None:
        return command, env
    entry = find_steam_runtime(proton)
    if entry is None:
        raise ProtonError(_runtime_missing_message(proton, appid))
    return [str(entry), "--verb=run", "--", *command], env


# What run_windows_tool runs through cmd.exe under Proton. The program, its arguments and the
# output file come in through environment variables, never as text in the .bat: Wine's cmd reads
# a .bat in the locale's OEM code page, which breaks non-ASCII paths, while it takes environment
# variables from UTF-8 correctly. cmd still parses the expanded values, so arguments are quoted.
_RUN_BAT = '@"%AZ_EXE%" %AZ_ARGS% > "%AZ_OUT%" 2>&1\r\n@exit /b %ERRORLEVEL%\r\n'


def _quote_arg(arg: str) -> str:
    """One argument for a Windows command line (MS C runtime rules), quoted when cmd or the program would split it.

    Quoted when it is empty or holds whitespace or a cmd operator. A run of
    backslashes right before the closing quote is doubled so it does not
    escape the quote: P:\\My Dir\\ becomes "P:\\My Dir\\\\".
    """
    if arg and not re.search(r"[\s&|<>^()]", arg):
        return arg
    trailing = "\\" * (len(arg) - len(arg.rstrip("\\")))
    return f'"{arg}{trailing}"'


def _check_cmd_value(what: str, value: str) -> None:
    """Raise ProtonError when value cannot pass through Wine's cmd.exe intact.

    Wine's cmd expands % again inside the values of AZ_EXE, AZ_ARGS and
    AZ_OUT (Windows' cmd does not), and a " or a line break would end the
    quoting or the command line.
    """
    if "%" in value:
        raise ProtonError(f"the {what} {value!r} contains %: paths and arguments containing % are not supported "
                          "under Proton. Rename the folder, or leave out the %.")
    if '"' in value or "\n" in value or "\r" in value:
        raise ProtonError(f"the {what} {value!r} contains a double quote or a line break, which cannot be passed "
                          "to a Windows program under Proton. Rename it.")


def run_windows_tool(exe, args: list[str], *, cwd=None, timeout=None) -> tuple[int, str]:
    """Run the Windows program exe to the end; returns (exit code, its stdout and stderr together).

    On Linux, Proton gives the program its own console, so its output never
    reaches this process. It runs instead through cmd.exe and a fixed .bat that
    redirects the output to a file, both in a temporary folder that is removed
    afterwards. Because the command goes through Wine's cmd, the program path,
    the output path and every argument must hold no %, " or line break: those
    raise ProtonError before anything starts. Non-ASCII characters are fine.
    Raises ProtonError like windows_tool_cmd, also when Proton fails before the
    program starts, and subprocess.TimeoutExpired.
    """
    if IS_WINDOWS:
        argv, _ = windows_tool_cmd(exe, args)
        result = subprocess.run(argv, cwd=cwd, timeout=timeout, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return result.returncode, result.stdout.decode("utf-8", errors="replace")
    wine_exe = to_wine_path(Path(exe))
    _check_cmd_value("program path", wine_exe)
    for arg in args:
        _check_cmd_value("argument", arg)
    with tempfile.TemporaryDirectory(prefix="agentic-z-proton-") as tmp:
        bat = Path(tmp) / "run.bat"
        output = Path(tmp) / "output.txt"
        wine_output = to_wine_path(output)
        _check_cmd_value("temporary output path", wine_output)  # TMPDIR is the user's
        bat.write_bytes(_RUN_BAT.encode("ascii"))
        argv, env = windows_tool_cmd("cmd.exe", ["/c", to_wine_path(bat)])
        env = {**os.environ, **env, "AZ_EXE": wine_exe, "AZ_ARGS": " ".join(_quote_arg(a) for a in args),
               "AZ_OUT": wine_output}
        result = subprocess.run(argv, cwd=cwd, timeout=timeout, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            # The tools may write cp1252; replace what is not UTF-8 rather than fail.
            text = output.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            # The program never started (Proton failed before it). Raise rather than return
            # Proton's exit code, so callers never mistake it for the program's own answer.
            proton_lines = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
            said = "\n".join(proton_lines[-5:]) or "(nothing)"
            raise ProtonError(f"{Path(exe).name} did not start under Proton (exit code "
                              f"{result.returncode}). Proton said:\n{said}")
    return result.returncode, text


def launch_windows_tool(exe, args: list[str], *, cwd=None) -> subprocess.Popen:
    """Start the Windows GUI program exe detached (Workbench, Object Builder) and return at once.

    Its output goes nowhere: a detached child holding this process's stdout
    would keep the caller's pipe open after this script exits.
    Raises ProtonError like windows_tool_cmd.
    """
    argv, env = windows_tool_cmd(exe, args)
    return subprocess.Popen(argv, cwd=cwd, env={**os.environ, **env} if env else None, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **detach_kwargs())


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

    # Only the build and launch skills run Proton, so a missing one is a warning.
    proton = find_proton()
    runtime_appid = required_runtime_appid(proton) if proton is not None else None
    if proton is None:
        add("warn", "proton", _proton_missing_message())
    elif runtime_appid is not None and find_steam_runtime(proton) is None:
        add("warn", "steam_runtime", _runtime_missing_message(proton, runtime_appid))
    else:
        add("ok", "proton", f"Proton for DayZ Tools: {proton}")
    sandboxed = _tools_install_kind(prefix)
    if sandboxed in ("flatpak", "snap"):
        add("warn", "proton_sandbox", f"DayZ Tools run in the Steam {sandboxed.capitalize()}; "
                                      "starting Proton from outside the Steam sandbox is not supported yet, so the "
                                      "build and launch skills cannot run the tools. Use a native Steam install.")

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
