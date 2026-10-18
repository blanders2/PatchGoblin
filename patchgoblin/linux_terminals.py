"""Linux terminal backends: named, coloured project tabs that can be found and refocused.

Each backend can ``list_tags()`` (the project ids of PatchGoblin's open tabs), ``focus(tag)``
and ``open(argv, cwd, title, color, tag)``. Tabs are matched on an exact ``PATCHGOBLIN_TAB``
environment value, never on tab order, so dragging tabs around can't focus the wrong one.
Commands are always argv lists, so the user's shell (fish included) never parses them.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
import time
from collections.abc import Callable

from . import hosts
from .hosts import HostError, communicate

TAG_ENV = "PATCHGOBLIN_TAB"
BACKENDS = ("auto", "terminalgoblin", "kitty", "tmux", "wezterm", "window")
_COLOR_OK = re.compile(r"^#[0-9a-fA-F]{6}$")
Which = Callable[[str], str | None]

KITTY_HELP = ("kitty's remote control is off. Add `allow_remote_control socket-only` and "
              "`listen_on unix:/tmp/kitty` to ~/.config/kitty/kitty.conf and restart kitty, "
              "or set Settings → Terminal → kitty socket.")


def parse_environ(data: bytes) -> dict[str, str]:
    """``/proc/<pid>/environ`` contents (NUL-separated ``KEY=value``) as a dict."""
    out = {}
    for item in data.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and key:
            out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def proc_tags(root: str = "/proc") -> dict[str, list[int]]:
    """Every running process that has PATCHGOBLIN_TAB in its environment: ``{tag: [pid, ...]}``.
    Works with any terminal emulator. Processes of other users and vanished ones are skipped."""
    out: dict[str, list[int]] = {}
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(root, name, "environ"), "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if TAG_ENV.encode() not in data:
            continue
        tag = parse_environ(data).get(TAG_ENV)
        if tag:
            out.setdefault(tag, []).append(int(name))
    return out


def is_wayland(env=os.environ) -> bool:
    return bool(env.get("WAYLAND_DISPLAY")) or env.get("XDG_SESSION_TYPE") == "wayland"


def _parent_pid(pid: int, root: str = "/proc") -> int:
    try:
        with open(os.path.join(root, str(pid), "stat"), "rb") as fh:
            # "pid (comm) state ppid ...": comm may contain spaces and parentheses
            return int(fh.read().rsplit(b")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def raise_x11(window_id: int | None = None, pid: int | None = None, which: Which = shutil.which) -> bool:
    """Best effort: bring a window to the front on X11 with xdotool or wmctrl, given its window
    id or the pid of a process that owns it (parents are tried too). Wayland compositors don't
    let an app do this, so nothing happens there. True if a tool was run."""
    if is_wayland() or not os.environ.get("DISPLAY"):
        return False
    xdotool, wmctrl = which("xdotool"), which("wmctrl")
    if not (xdotool or wmctrl):
        return False
    if window_id is None and pid and xdotool:
        for _ in range(8):
            if pid <= 1:
                break
            res = communicate([xdotool, "search", "--pid", str(pid)], timeout=5)
            ids = res.stdout.split() if res.ok else []
            if ids:
                window_id = int(ids[-1])
                break
            pid = _parent_pid(pid)
    if window_id is None:
        return False
    if xdotool:
        argv = [xdotool, "windowactivate", str(window_id)]
    else:
        argv = [wmctrl, "-i", "-a", hex(window_id)]
    return communicate(argv, timeout=5).ok


def parse_kitty_ls(text: str) -> list[tuple[str, int | None]]:
    """``(tag, platform_window_id)`` for each window of ``kitty @ ls`` JSON that carries a tag."""
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    out = []
    for osw in data if isinstance(data, list) else []:
        for tab in osw.get("tabs") or []:
            for win in tab.get("windows") or []:
                tag = (win.get("env") or {}).get(TAG_ENV)
                if tag:
                    wid = osw.get("platform_window_id")
                    out.append((tag, wid if isinstance(wid, int) else None))
    return out


def _check_tag(tag: str) -> str:
    return tag if tag and hosts._TAG_OK.match(tag) else ""


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


class KittyBackend:
    name = "kitty"

    def __init__(self, socket: str = "", which: Which = shutil.which):
        self.socket = (socket or "").strip()
        self.which = which

    # -- finding kitty ------------------------------------------------------
    def _candidates(self) -> list[str]:
        out = [self.socket, os.environ.get("KITTY_LISTEN_ON", "")]
        runtime = _runtime_dir()
        paths = glob.glob("/tmp/kitty*") + glob.glob(os.path.join(runtime, "kitty*")) \
            + glob.glob(os.path.join(runtime, "patchgoblin-kitty*"))
        socks = []
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            if stat.S_ISSOCK(st.st_mode):
                socks.append((st.st_mtime, p))
        out += [f"unix:{p}" for _, p in sorted(socks, reverse=True)]
        seen = set()
        return [a for a in out if a and not (a in seen or seen.add(a))]

    def find(self) -> tuple[str, str]:
        """``(address, error)``: a kitty instance with remote control on, or why there is none.
        Both empty means no kitty is listening at all."""
        error = ""
        for addr in self._candidates():
            res = communicate(["kitty", "@", "--to", addr, "ls"], timeout=5)
            if res.ok:
                return addr, ""
            low = res.stderr.lower()
            if "disabled" in low or "allow_remote_control" in low:
                error = KITTY_HELP
        return "", error

    def _rc(self, addr: str, *args: str):
        return communicate(["kitty", "@", "--to", addr, *args], timeout=5)

    def probe(self) -> tuple[bool, str]:
        if not self.which("kitty"):
            return False, "kitty was not found on PATH."
        addr, error = self.find()
        if addr:
            return True, "kitty remote control is on."
        if error:
            return False, error
        return True, "No kitty is running with remote control; PatchGoblin will start its own."

    # -- the backend interface ----------------------------------------------
    def _need_addr(self) -> str:
        addr, error = self.find()
        if error and not addr:
            raise HostError(error)
        return addr

    def list_tags(self) -> list[str]:
        addr, error = self.find()
        if error and not addr:
            raise HostError(error)
        if not addr:
            return []
        res = self._rc(addr, "ls")
        return [tag for tag, _ in parse_kitty_ls(res.stdout)] if res.ok else []

    def focus(self, tag: str) -> None:
        addr = self._need_addr()
        res = self._rc(addr, "focus-tab", "--match", f"env:{TAG_ENV}={tag}")
        if not res.ok:
            raise HostError(f"kitty could not focus the tab: {res.stderr.strip() or res.returncode}")
        if not is_wayland():
            ls = self._rc(addr, "ls")
            for t, wid in parse_kitty_ls(ls.stdout):
                if t == tag and wid:
                    raise_x11(window_id=wid, which=self.which)
                    break

    def _color(self, addr: str, tag: str, color: str) -> None:
        if tag and _COLOR_OK.match(color or ""):
            self._rc(addr, "set-tab-color", "--match", f"env:{TAG_ENV}={tag}",
                     f"active_bg={color}", f"inactive_bg={color}")

    def open(self, argv: list[str] | None, cwd: str, title: str, color: str, tag: str) -> None:
        if not self.which("kitty"):
            raise HostError("kitty was not found on PATH.")
        tag = _check_tag(tag)
        addr = self._need_addr()
        if not addr:
            self._start(argv, cwd, title, color, tag)
            return
        cmd = ["launch", "--type=tab", "--cwd", cwd]
        if title:
            cmd += ["--tab-title", title]
        if tag:
            cmd += ["--env", f"{TAG_ENV}={tag}"]
        if argv:
            cmd += ["--", *argv]
        res = self._rc(addr, *cmd)
        if not res.ok:
            raise HostError(f"kitty could not open a tab: {res.stderr.strip() or res.returncode}")
        self._color(addr, tag, color)
        if not is_wayland():
            ls = self._rc(addr, "ls")
            for t, wid in parse_kitty_ls(ls.stdout):
                if t == tag and wid:
                    raise_x11(window_id=wid, which=self.which)
                    break

    def _start(self, argv, cwd, title, color, tag) -> None:
        """Start a kitty with remote control on (by flags, so kitty.conf needs no edits) whose
        one tab is the project's."""
        lines = [f"new_tab {title}" if title else "new_tab", f"cd {cwd}"]
        launch = ["launch"] + ([f"--env", f"{TAG_ENV}={tag}"] if tag else []) + (argv or [])
        lines.append(shlex.join(launch))
        fd, path = tempfile.mkstemp(prefix="patchgoblin-kitty-", suffix=".session")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            sock = os.path.join(_runtime_dir(), "patchgoblin-kitty")
            hosts._spawn(["kitty", "-o", "allow_remote_control=socket-only",
                          "--listen-on", f"unix:{sock}", "--session", path], None, "CREATE_NEW_CONSOLE")
            addr = ""
            for _ in range(25):
                addr, _err = self.find()
                if addr:
                    break
                time.sleep(0.2)
            if addr:
                self._color(addr, tag, color)
        finally:
            # kitty reads the session at start-up; give it a moment before it goes away
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


class TmuxBackend:
    name = "tmux"
    session = hosts.TERMINAL_WINDOW

    def __init__(self, which: Which = shutil.which):
        self.which = which

    def _tmux(self, *args: str):
        return communicate(["tmux", *args], timeout=10)

    def probe(self) -> tuple[bool, str]:
        if not self.which("tmux"):
            return False, "tmux was not found on PATH."
        return True, "tmux session “PatchGoblin”; attach to it from any terminal."

    def _has_session(self) -> bool:
        return self._tmux("has-session", "-t", self.session).ok

    def _windows(self) -> list[tuple[str, str]]:
        res = self._tmux("list-windows", "-t", self.session, "-F", "#{window_id}\t#{@patchgoblin_tab}")
        if not res.ok:
            return []
        return [tuple(line.split("\t", 1)) for line in res.stdout.splitlines() if "\t" in line]

    def list_tags(self) -> list[str]:
        return [tag for _, tag in self._windows() if tag]

    def _check_version(self) -> None:
        res = self._tmux("-V")
        m = re.search(r"(\d+)\.(\d+)", res.stdout or "")
        if res.ok and m and (int(m.group(1)), int(m.group(2))) < (3, 0):
            raise HostError("tmux 3.0 or newer is needed (found " + res.stdout.strip() + ").")

    def _show(self) -> None:
        """Make sure a terminal window is showing the session (opening one if nobody is attached)."""
        res = self._tmux("list-clients", "-t", self.session, "-F", "#{client_pid}")
        pids = [int(p) for p in res.stdout.split() if p.isdigit()] if res.ok else []
        if not pids:
            argv, cwd = hosts._wrap_terminal(["tmux", "attach", "-t", self.session], None, "linux", self.which)
            hosts._spawn(argv, cwd, "CREATE_NEW_CONSOLE")
        else:
            raise_x11(pid=pids[0], which=self.which)

    def focus(self, tag: str) -> None:
        for wid, t in self._windows():
            if t == tag:
                res = self._tmux("select-window", "-t", wid)
                if not res.ok:
                    raise HostError(f"tmux could not select the window: {res.stderr.strip()}")
                self._show()
                return
        raise HostError("That tmux window is gone.")

    def open(self, argv: list[str] | None, cwd: str, title: str, color: str, tag: str) -> None:
        if not self.which("tmux"):
            raise HostError("tmux was not found on PATH.")
        self._check_version()
        tag = _check_tag(tag)
        base = ["-n", title or "shell", "-c", cwd]
        if tag:
            base += ["-e", f"{TAG_ENV}={tag}"]
        base += ["-P", "-F", "#{window_id}"]
        if self._has_session():
            cmd = ["new-window", "-t", f"{self.session}:", *base]
        else:
            cmd = ["new-session", "-d", "-s", self.session, *base]
        res = self._tmux(*cmd, *(argv or []))
        wid = res.stdout.strip()
        if not res.ok or not wid:
            raise HostError(f"tmux could not open a window: {res.stderr.strip() or res.returncode}")
        # Per-window options: the tag for look-ups, and no renaming by the shell (fish_title,
        # escape sequences) or by tmux itself.
        opts = [("@patchgoblin_tab", tag)] if tag else []
        opts += [("automatic-rename", "off"), ("allow-rename", "off")]
        if _COLOR_OK.match(color or ""):
            opts += [("window-status-style", f"bg={color}"), ("window-status-current-style", f"bg={color}")]
        for name, value in opts:
            self._tmux("set-option", "-w", "-t", wid, name, value)
        self._tmux("select-window", "-t", wid)
        self._show()


class WeztermBackend:
    """WezTerm: tabs are found by joining ``wezterm cli list`` (tty names) with the tagged
    processes' terminals. WezTerm's CLI can't colour a tab."""

    name = "wezterm"

    def __init__(self, which: Which = shutil.which):
        self.which = which

    def probe(self) -> tuple[bool, str]:
        if not self.which("wezterm"):
            return False, "wezterm was not found on PATH."
        return True, "WezTerm (tabs are named but not coloured)."

    def _panes(self) -> list[dict]:
        res = communicate(["wezterm", "cli", "list", "--format", "json"], timeout=10)
        try:
            panes = json.loads(res.stdout) if res.ok else []
        except ValueError:
            return []
        return panes if isinstance(panes, list) else []

    def _tabs(self) -> dict[str, int]:
        """``{tag: tab_id}`` for the open tagged tabs."""
        by_tty = {p.get("tty_name"): p.get("tab_id") for p in self._panes() if p.get("tty_name")}
        out = {}
        for tag, pids in proc_tags().items():
            for pid in pids:
                try:
                    tty = os.readlink(f"/proc/{pid}/fd/0")
                except OSError:
                    continue
                if tty in by_tty:
                    out[tag] = by_tty[tty]
                    break
        return out

    def list_tags(self) -> list[str]:
        return list(self._tabs())

    def focus(self, tag: str) -> None:
        tab = self._tabs().get(tag)
        if tab is None:
            raise HostError("That WezTerm tab is gone.")
        res = communicate(["wezterm", "cli", "activate-tab", "--tab-id", str(tab)], timeout=10)
        if not res.ok:
            raise HostError(f"WezTerm could not switch tab: {res.stderr.strip() or res.returncode}")

    def open(self, argv: list[str] | None, cwd: str, title: str, color: str, tag: str) -> None:
        if not self.which("wezterm"):
            raise HostError("wezterm was not found on PATH.")
        tag = _check_tag(tag)
        run = ["env", f"{TAG_ENV}={tag}"] if tag else ["env"]
        run += argv or [os.environ.get("SHELL") or "/bin/sh", "-l"]
        res = communicate(["wezterm", "cli", "spawn", "--cwd", cwd, "--", *run], timeout=10)
        pane = res.stdout.strip()
        if res.ok and pane.isdigit():
            if title:
                communicate(["wezterm", "cli", "set-tab-title", "--pane-id", pane, title], timeout=10)
            return
        # no WezTerm window to add a tab to: start one
        hosts._spawn(["wezterm", "start", "--cwd", cwd, "--", *run], None, "CREATE_NEW_CONSOLE")


def parse_terminalgoblin_ls(text: str) -> list[str]:
    """The tags of the panes in ``terminalgoblin @ ls`` JSON (a list of running instances)."""
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    out = []
    for inst in data if isinstance(data, list) else []:
        for tab in (inst.get("tabs") or []) if isinstance(inst, dict) else []:
            for pane in tab.get("panes") or []:
                tag = (pane.get("env") or {}).get(TAG_ENV)
                if tag and tag not in out:
                    out.append(tag)
    return out


class TerminalGoblinBackend:
    """TerminalGoblin: named, coloured tabs through its remote control (``terminalgoblin @ ...``).
    Panes are tagged with PATCHGOBLIN_TAB in their launch environment, so a project's terminal is
    found even after it has been moved into another tab as a split. ``@ launch`` starts
    TerminalGoblin when it isn't running, and the title it's given can't be changed by the shell."""

    name = "terminalgoblin"
    NOT_FOUND = "TerminalGoblin was not found on PATH (install it with its install.sh)."

    def __init__(self, which: Which = shutil.which):
        self.which = which

    def exe(self) -> str | None:
        # The systemd service's PATH may predate ~/.local/bin, where install.sh puts it.
        return self.which("terminalgoblin") or self.which(os.path.expanduser("~/.local/bin/terminalgoblin"))

    def _rc(self, *args: str, timeout: float = 15):
        exe = self.exe()
        if not exe:
            raise HostError(self.NOT_FOUND)
        return communicate([exe, "@", *args], timeout=timeout)

    def probe(self) -> tuple[bool, str]:
        if not self.exe():
            return False, self.NOT_FOUND
        return True, "TerminalGoblin (named, coloured tabs; starts it if it isn't running)."

    def list_tags(self) -> list[str]:
        res = self._rc("ls")
        return parse_terminalgoblin_ls(res.stdout) if res.ok else []

    def focus(self, tag: str) -> None:
        res = self._rc("focus", "--match", f"env:{TAG_ENV}={tag}")
        if not res.ok:
            raise HostError(f"TerminalGoblin could not focus the tab: {res.stderr.strip() or res.returncode}")

    def open(self, argv: list[str] | None, cwd: str, title: str, color: str, tag: str) -> None:
        tag = _check_tag(tag)
        # An SSH project's path is on the remote host (argv cds there), so it's only a local cwd
        # for local projects.
        cmd = ["launch"] + ([] if argv else ["--cwd", cwd])
        if title:
            cmd += ["--title", title]
        if _COLOR_OK.match(color or ""):
            cmd += ["--color", color]
        if tag:
            cmd += ["--env", f"{TAG_ENV}={tag}"]
        if argv:
            cmd += ["--", *argv]
        res = self._rc(*cmd, timeout=20)
        if not res.ok:
            raise HostError(f"TerminalGoblin could not open a tab: {res.stderr.strip() or res.returncode}")


def resolve_backend(terminal: dict | None, which: Which = shutil.which):
    """The backend for the ``terminal`` settings, or None for a plain new window each time.
    ``auto``: TerminalGoblin if installed, else a reachable kitty, else tmux, else a kitty
    PatchGoblin starts, else a plain window."""
    terminal = terminal or {}
    choice = terminal.get("linux_backend") or "auto"
    kitty = KittyBackend(terminal.get("kitty_socket") or "", which)
    goblin = TerminalGoblinBackend(which)
    if choice == "terminalgoblin":
        return goblin
    if choice == "kitty":
        return kitty
    if choice == "tmux":
        return TmuxBackend(which)
    if choice == "wezterm":
        return WeztermBackend(which)
    if choice != "auto":
        return None
    if goblin.exe():
        return goblin
    if which("kitty"):
        addr, error = kitty.find()
        if addr or error:
            return kitty
    if which("tmux"):
        return TmuxBackend(which)
    if which("kitty"):
        return kitty
    return None
