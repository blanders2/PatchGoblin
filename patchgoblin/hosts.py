"""Where a project lives: a directory on this machine or on an SSH host.

Both host types expose the same small interface (read/write text files, run
commands in a directory) so the rest of the app never cares which one it has.
Remote hosts are driven with the system ``ssh`` client, so keys, agents and
``~/.ssh/config`` aliases all work as they do in a terminal.
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
import shutil
import signal
import string
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

OutputFn = Optional[Callable[[str], None]]
StartFn = Optional[Callable[[subprocess.Popen], None]]


class HostError(RuntimeError):
    pass


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def kill_tree(proc: subprocess.Popen) -> None:
    """Terminate a process and its children (CLI agents spawn helpers)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def communicate(argv, cwd=None, input: Optional[str] = None, timeout: Optional[float] = None,
                on_output: OutputFn = None, on_start: StartFn = None, shell: bool = False,
                env: Optional[dict] = None) -> Result:
    """Run a process, streaming stdout+stderr lines to ``on_output`` as they arrive.

    ``env`` holds extra environment variables added to this process's own environment.
    """
    kwargs = {}
    if env:
        kwargs["env"] = {**os.environ, **env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, shell=shell,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    except FileNotFoundError:
        name = argv if isinstance(argv, str) else argv[0]
        return Result(127, "", f"Command not found: {name}\n")
    except OSError as exc:
        return Result(126, "", f"Could not start process: {exc}\n")
    if on_start:
        on_start(proc)

    out: list[str] = []
    err: list[str] = []

    def pump(stream, sink):
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", "replace")
            sink.append(text)
            if on_output:
                on_output(text)
        stream.close()

    pumps = [threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
             threading.Thread(target=pump, args=(proc.stderr, err), daemon=True)]
    for t in pumps:
        t.start()
    if input is not None:
        try:
            proc.stdin.write(input.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
    for t in pumps:
        t.join(timeout=5)
    return Result(proc.returncode if proc.returncode is not None else -1,
                  "".join(out), "".join(err), timed_out)


class LocalHost:
    kind = "local"
    label = "this computer"

    def join(self, *parts: str) -> str:
        return os.path.join(*parts)

    def normalize(self, path: str) -> str:
        path = os.path.expanduser(path.strip())
        if not os.path.isabs(path):
            raise HostError("Use an absolute directory path.")
        return os.path.normpath(path)

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def ensure_dir(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def home(self) -> str:
        return os.path.expanduser("~")

    def list_dirs(self, path: str) -> dict:
        """Subdirectories of ``path``. On Windows, an empty path lists the drives."""
        if not path and os.name == "nt":
            drives = os.listdrives() if hasattr(os, "listdrives") else [
                f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
            return {"path": "", "parent": None, "sep": os.sep,
                    "dirs": [{"name": d, "path": d} for d in drives]}
        path = self.normalize(path)
        if not os.path.isdir(path):
            raise HostError(f"Not a directory: {path}")
        dirs = []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir():
                            dirs.append({"name": entry.name, "path": entry.path})
                    except OSError:
                        continue
        except OSError as exc:
            raise HostError(f"Cannot open {path}: {exc.strerror or exc}") from exc
        parent = os.path.dirname(path)
        if parent == path:  # filesystem root
            parent = "" if os.name == "nt" else None
        dirs.sort(key=lambda d: d["name"].lower())
        return {"path": path, "parent": parent, "sep": os.sep, "dirs": dirs}

    def read_text(self, path: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HostError(f"Could not read {path}: {exc}") from exc

    def list_dir(self, path: str) -> list[str]:
        """Names of the entries in ``path``; [] if it is not a directory."""
        try:
            return sorted(os.listdir(path))
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError as exc:
            raise HostError(f"Could not list {path}: {exc}") from exc

    def write_text(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        # Sync clients and virus scanners briefly hold files open on Windows.
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def run(self, argv: list[str], cwd: str, input: Optional[str] = None,
            timeout: Optional[float] = None, on_output: OutputFn = None,
            on_start: StartFn = None, login: bool = False, env: Optional[dict] = None) -> Result:
        exe = shutil.which(argv[0]) or argv[0]
        return communicate([exe, *argv[1:]], cwd=cwd, input=input, timeout=timeout,
                           on_output=on_output, on_start=on_start, env=env)

    def run_shell(self, command: str, cwd: str, timeout: Optional[float] = None,
                  on_output: OutputFn = None, on_start: StartFn = None) -> Result:
        return communicate(command, cwd=cwd, timeout=timeout, on_output=on_output,
                           on_start=on_start, shell=True)


_TARGET_RE = re.compile(r"^[A-Za-z0-9_.@\-\[\]:%]+$")


class SSHHost:
    """A POSIX host reached with the system ``ssh`` client (key auth, BatchMode)."""

    kind = "ssh"

    def __init__(self, target: str, port: Optional[int] = None, ssh_bin: str = "ssh"):
        target = (target or "").strip()
        if not target or target.startswith("-") or not _TARGET_RE.match(target):
            raise HostError("SSH target must look like user@host or an ssh-config alias.")
        self.target = target
        self.port = int(port) if port else None
        self.ssh_bin = ssh_bin
        self.label = target if not self.port else f"{target}:{self.port}"

    def _argv(self, remote_command: str) -> list[str]:
        argv = [self.ssh_bin, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                "-o", "ServerAliveInterval=30"]
        if self.port:
            argv += ["-p", str(self.port)]
        return argv + [self.target, remote_command]

    def _exec(self, script: str, **kw) -> Result:
        return communicate(self._argv(script), **kw)

    @staticmethod
    def _login(script: str) -> str:
        # Login shell so PATH additions from ~/.profile (npm, nvm, ~/.local/bin) apply.
        return f'exec "${{SHELL:-/bin/sh}}" -lc {shlex.quote(script)}'

    def join(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def normalize(self, path: str) -> str:
        path = path.strip()
        if not path.startswith("/"):
            raise HostError("Use an absolute POSIX path on the remote host, e.g. /home/me/project.")
        return posixpath.normpath(path)

    def check(self) -> None:
        res = self._exec("true", timeout=30)
        if not res.ok:
            raise HostError(f"SSH connection to {self.label} failed: {res.stderr.strip() or res.returncode}")

    def is_dir(self, path: str) -> bool:
        return self._exec(f"test -d {shlex.quote(path)}", timeout=60).ok

    def ensure_dir(self, path: str) -> None:
        res = self._exec(f"mkdir -p {shlex.quote(path)}", timeout=60)
        if not res.ok:
            raise HostError(f"Could not create {path} on {self.label}: {res.stderr.strip()}")

    def home(self) -> str:
        res = self._exec('printf "%s" "$HOME"', timeout=30)
        if not res.ok or not res.stdout.startswith("/"):
            raise HostError(f"Could not find the home directory on {self.label}: {res.stderr.strip()}")
        return res.stdout

    def list_dirs(self, path: str) -> dict:
        path = self.normalize(path)
        script = (f"cd {shlex.quote(path)} 2>/dev/null || exit 45; pwd; "
                  "find -L . -mindepth 1 -maxdepth 1 -type d 2>/dev/null; exit 0")
        res = self._exec(script, timeout=60)
        if res.returncode == 45:
            raise HostError(f"Not a directory (or no access): {path}")
        if not res.ok:
            raise HostError(f"Could not list {path} on {self.label}: {res.stderr.strip() or 'ssh failed'}")
        lines = res.stdout.splitlines()
        cwd = lines[0] if lines else path
        names = sorted((ln[2:] for ln in lines[1:] if ln.startswith("./")), key=str.lower)
        return {"path": cwd, "parent": posixpath.dirname(cwd) if cwd != "/" else None, "sep": "/",
                "dirs": [{"name": n, "path": posixpath.join(cwd, n)} for n in names]}

    def read_text(self, path: str) -> Optional[str]:
        q = shlex.quote(path)
        res = self._exec(f"if [ -f {q} ]; then cat {q}; else exit 44; fi", timeout=60)
        if res.returncode == 44:
            return None
        if not res.ok:
            raise HostError(f"Could not read {path} on {self.label}: {res.stderr.strip() or 'ssh failed'}")
        return res.stdout

    def list_dir(self, path: str) -> list[str]:
        q = shlex.quote(path)
        res = self._exec(f"if [ -d {q} ]; then ls -1A {q}; fi", timeout=60)
        if not res.ok:
            raise HostError(f"Could not list {path} on {self.label}: {res.stderr.strip() or 'ssh failed'}")
        return sorted(line for line in res.stdout.splitlines() if line)

    def write_text(self, path: str, text: str) -> None:
        q, tmp = shlex.quote(path), shlex.quote(f"{path}.tmp")
        d = shlex.quote(posixpath.dirname(path))
        res = self._exec(f"mkdir -p {d} && cat > {tmp} && mv -f {tmp} {q}", input=text, timeout=60)
        if not res.ok:
            raise HostError(f"Could not write {path} on {self.label}: {res.stderr.strip() or 'ssh failed'}")

    def run(self, argv: list[str], cwd: str, input: Optional[str] = None,
            timeout: Optional[float] = None, on_output: OutputFn = None,
            on_start: StartFn = None, login: bool = False, env: Optional[dict] = None) -> Result:
        prefix = ["env", *(f"{k}={v}" for k, v in env.items())] if env else []
        script = f"cd {shlex.quote(cwd)} && " + " ".join(shlex.quote(a) for a in [*prefix, *argv])
        if login:
            script = self._login(script)
        return self._exec(script, input=input, timeout=timeout, on_output=on_output, on_start=on_start)

    def run_shell(self, command: str, cwd: str, timeout: Optional[float] = None,
                  on_output: OutputFn = None, on_start: StartFn = None) -> Result:
        script = self._login(f"cd {shlex.quote(cwd)} && {command}")
        return self._exec(script, timeout=timeout, on_output=on_output, on_start=on_start)


    def shell_argv(self, path: str) -> list[str]:
        """ssh command for an interactive login shell in ``path`` (for a terminal window)."""
        argv = [self.ssh_bin, "-t"]
        if self.port:
            argv += ["-p", str(self.port)]
        return argv + [self.target, f'cd {shlex.quote(path)} && exec "${{SHELL:-/bin/sh}}" -l']


_LINUX_TERMINALS = (
    # (binary, flags before the command to run inside it)
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("xterm", ["-e"]),
)


def terminal_command(host, path: str, platform: str = sys.platform,
                     which: Callable[[str], Optional[str]] = shutil.which) -> tuple[list[str], Optional[str]]:
    """Argv and working directory that open a new terminal window on this computer.

    Local projects get a shell in ``path``; SSH projects get a local terminal running
    ssh that lands in ``path`` on the remote host.
    """
    inner = host.shell_argv(path) if host.kind == "ssh" else None
    cwd = path if inner is None else None
    if platform == "win32":
        if which("wt"):
            # Windows Terminal treats ";" as a command separator, so escape it.
            if inner is None:
                return ["wt", "-d", path.replace(";", "\\;")], None
            return ["wt", *[a.replace(";", "\\;") for a in inner]], None
        return inner or ["powershell.exe", "-NoExit"], cwd
    if platform == "darwin":
        if inner is None:
            return ["open", "-a", "Terminal", path], None
        script = shlex.join(inner).replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                "-e", 'tell application "Terminal" to activate'], None
    for name, flags in _LINUX_TERMINALS:
        if which(name):
            return [name, *(flags + inner if inner else [])], cwd
    raise HostError("No terminal emulator found (tried " + ", ".join(n for n, _ in _LINUX_TERMINALS) + ").")


def open_terminal(project: dict) -> None:
    """Open a terminal window on this computer in the project's directory."""
    host = host_for(project)
    path = project["path"]
    if host.kind == "local" and not host.is_dir(path):
        raise HostError(f"Directory does not exist: {path}")
    argv, cwd = terminal_command(host, path)
    try:
        _spawn(argv, cwd, "CREATE_NEW_CONSOLE")
    except OSError as exc:
        raise HostError(f"Could not open a terminal: {exc}") from exc


def _spawn(argv: list[str], cwd: Optional[str], windows_flag: str) -> None:
    """Start ``argv`` detached without waiting; ``windows_flag`` names the subprocess creation flag."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, windows_flag)
    else:
        kwargs["start_new_session"] = True
    exe = shutil.which(argv[0]) or argv[0]
    subprocess.Popen([exe, *argv[1:]], cwd=cwd, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def vscode_command(host, path: str, platform: str = sys.platform,
                   which: Callable[[str], Optional[str]] = shutil.which) -> list[str]:
    """Argv that opens ``path`` in VS Code on this computer (via Remote-SSH for SSH projects)."""
    code = which("code")
    if not code and platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            candidate = os.path.join(local_app, "Programs", "Microsoft VS Code", "bin", "code.cmd")
            code = which(candidate)
    if host.kind == "ssh":
        if host.port:
            raise HostError("VS Code Remote-SSH needs an ~/.ssh/config Host alias for a custom port; "
                            "use the alias as the SSH target.")
        remote = ["--remote", f"ssh-remote+{host.target}", path]
    else:
        remote = [path]
    if code:
        return [code, *remote]
    if platform == "darwin" and host.kind != "ssh":
        return ["open", "-a", "Visual Studio Code", path]
    raise HostError("VS Code's 'code' command was not found on PATH. "
                    "In VS Code run \"Shell Command: Install 'code' command in PATH\".")


def open_vscode(project: dict) -> None:
    """Open the project's directory in VS Code on this computer."""
    host = host_for(project)
    path = project["path"]
    if host.kind == "local" and not host.is_dir(path):
        raise HostError(f"Directory does not exist: {path}")
    argv = vscode_command(host, path)
    try:
        _spawn(argv, None, "CREATE_NO_WINDOW")
    except OSError as exc:
        raise HostError(f"Could not open VS Code: {exc}") from exc


def host_for(project: dict):
    if project.get("location") == "ssh":
        return SSHHost(project.get("ssh_target", ""), project.get("ssh_port"))
    return LocalHost()


def probe(project: dict) -> dict:
    """Whether the project's directory can be reached right now: ``{"ok", "error"}``. Never raises."""
    path = project.get("path", "")
    try:
        host = host_for(project)
        if host.kind == "local":
            if os.path.isdir(path):
                return {"ok": True, "error": ""}
            return {"ok": False, "error": f"Directory does not exist: {path}"}
        res = host._exec(f"test -d {shlex.quote(path)}", timeout=20)
        if res.timed_out:
            return {"ok": False, "error": f"SSH connection to {host.label} timed out"}
        if res.returncode == 0:
            return {"ok": True, "error": ""}
        if res.returncode in (255, 126, 127):
            return {"ok": False, "error": f"SSH connection to {host.label} failed: "
                                          f"{res.stderr.strip() or res.returncode}"}
        return {"ok": False, "error": f"Directory does not exist on {host.label}: {path}"}
    except (HostError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc) or exc.__class__.__name__}
