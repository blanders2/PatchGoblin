"""Where a project lives: a directory on this machine or on an SSH host.

Both host types expose the same small interface (read/write text files, run
commands in a directory) so the rest of the app never cares which one it has.
Remote hosts are driven with the system ``ssh`` client, so keys, agents and
``~/.ssh/config`` aliases all work as they do in a terminal.
"""
from __future__ import annotations

import base64
import colorsys
import json
import ntpath
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
from collections.abc import Callable
from dataclasses import dataclass

OutputFn = Callable[[str], None] | None
StartFn = Callable[[subprocess.Popen], None] | None


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
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def background_kwargs() -> dict:
    """Popen kwargs for a child with no console window that kill_tree can take down whole."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def communicate(argv, cwd=None, input: str | None = None, timeout: float | None = None,
                on_output: OutputFn = None, on_start: StartFn = None, shell: bool = False,
                env: dict | None = None) -> Result:
    """Run a process, streaming stdout+stderr lines to ``on_output`` as they arrive.

    ``env`` holds extra environment variables added to this process's own environment.
    """
    kwargs = background_kwargs()
    if env:
        kwargs["env"] = {**os.environ, **env}
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
    lists_drives = os.name == "nt"

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

    @staticmethod
    def _read(path: str, mode: str, **kw):
        try:
            with open(path, mode, **kw) as fh:
                return fh.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HostError(f"Could not read {path}: {exc}") from exc

    def read_text(self, path: str) -> str | None:
        return self._read(path, "r", encoding="utf-8")

    def read_bytes(self, path: str) -> bytes | None:
        return self._read(path, "rb")

    @staticmethod
    def _write(path: str, data, mode: str, **kw) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, mode, **kw) as fh:
            fh.write(data)
        # Sync clients and virus scanners briefly hold files open on Windows.
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def write_bytes(self, path: str, data: bytes) -> None:
        self._write(path, data, "wb")

    def list_dir(self, path: str) -> list[str]:
        """Names of the entries in ``path``; [] if it is not a directory."""
        try:
            return sorted(os.listdir(path))
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError as exc:
            raise HostError(f"Could not list {path}: {exc}") from exc

    def write_text(self, path: str, text: str) -> None:
        self._write(path, text, "w", encoding="utf-8", newline="\n")

    def run(self, argv: list[str], cwd: str, input: str | None = None,
            timeout: float | None = None, on_output: OutputFn = None,
            on_start: StartFn = None, login: bool = False, env: dict | None = None) -> Result:
        exe = shutil.which(argv[0]) or argv[0]
        return communicate([exe, *argv[1:]], cwd=cwd, input=input, timeout=timeout,
                           on_output=on_output, on_start=on_start, env=env)

    def run_shell(self, command: str, cwd: str, timeout: float | None = None,
                  on_output: OutputFn = None, on_start: StartFn = None) -> Result:
        return communicate(command, cwd=cwd, timeout=timeout, on_output=on_output,
                           on_start=on_start, shell=True)


_TARGET_RE = re.compile(r"^[A-Za-z0-9_.@\-\[\]:%]+$")


def _pq(s: str) -> str:
    """Quote ``s`` so sh, bash, zsh and fish all read it as the same string: runs of other
    characters go in single quotes, ``'`` and ``\\`` in double quotes (a backslash means
    different things to them inside single quotes). sshd hands the remote command to the user's
    login shell, which may be fish."""
    return "".join('"\'"' if c == "'" else '"\\\\"' if c == "\\" else f"'{c}'"
                   for c in re.findall(r"[^'\\]+|'|\\", s)) or "''"


class SSHHost:
    """A POSIX host reached with the system ``ssh`` client (key auth, BatchMode)."""

    kind = "ssh"
    os = "posix"
    noop_script = "true"

    def __init__(self, target: str, port: int | None = None):
        target = (target or "").strip()
        if not target or target.startswith("-") or not _TARGET_RE.match(target):
            raise HostError("SSH target must look like user@host or an ssh-config alias.")
        self.target = target
        self.port = int(port) if port else None
        self.label = target if not self.port else f"{target}:{self.port}"

    def _argv(self, remote_command: str) -> list[str]:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                "-o", "ServerAliveInterval=30"]
        return argv + self._port_args() + [self.target, remote_command]

    def _exec(self, script: str, **kw) -> Result:
        # Always run in sh: the remote login shell may be fish, which can't parse POSIX scripts.
        return communicate(self._argv("sh -c " + _pq(script)), **kw)

    def _fail(self, res: Result, verb: str, path: str) -> HostError:
        return HostError(f"Could not {verb} {path} on {self.label}: {res.stderr.strip() or 'ssh failed'}")

    def _port_args(self) -> list[str]:
        return ["-p", str(self.port)] if self.port else []

    @staticmethod
    def _login(script: str) -> str:
        # Login shell so PATH additions from ~/.profile (npm, nvm, ~/.local/bin) apply; the script
        # itself still runs in sh, whatever the login shell is ("$PG_SCRIPT" reads the same in
        # sh, bash, zsh and fish).
        return (f'exec env PG_SCRIPT={shlex.quote(script)} "${{SHELL:-/bin/sh}}" '
                '-lc \'exec sh -c "$PG_SCRIPT"\'')

    def join(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def normalize(self, path: str) -> str:
        path = path.strip()
        if not path.startswith("/"):
            raise HostError("Use an absolute POSIX path on the remote host, e.g. /home/me/project.")
        return posixpath.normpath(path)

    def check(self) -> None:
        res = self._exec(self.noop_script, timeout=30)
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
            raise self._fail(res, "list", path)
        lines = res.stdout.splitlines()
        cwd = lines[0] if lines else path
        names = sorted((ln[2:] for ln in lines[1:] if ln.startswith("./")), key=str.lower)
        return {"path": cwd, "parent": posixpath.dirname(cwd) if cwd != "/" else None, "sep": "/",
                "dirs": [{"name": n, "path": posixpath.join(cwd, n)} for n in names]}

    def read_text(self, path: str) -> str | None:
        q = shlex.quote(path)
        res = self._exec(f"if [ -f {q} ]; then cat {q}; else exit 44; fi", timeout=60)
        if res.returncode == 44:
            return None
        if not res.ok:
            raise self._fail(res, "read", path)
        return res.stdout

    def list_dir(self, path: str) -> list[str]:
        q = shlex.quote(path)
        res = self._exec(f"if [ -d {q} ]; then ls -1A {q}; fi", timeout=60)
        if not res.ok:
            raise self._fail(res, "list", path)
        return sorted(line for line in res.stdout.splitlines() if line)

    def write_text(self, path: str, text: str) -> None:
        q, tmp = shlex.quote(path), shlex.quote(f"{path}.tmp")
        d = shlex.quote(posixpath.dirname(path))
        res = self._exec(f"mkdir -p {d} && cat > {tmp} && mv -f {tmp} {q}", input=text, timeout=60)
        if not res.ok:
            raise self._fail(res, "write", path)

    def read_bytes(self, path: str) -> bytes | None:
        q = shlex.quote(path)
        res = self._exec(f"if [ -f {q} ]; then base64 < {q}; else exit 44; fi", timeout=120)
        if res.returncode == 44:
            return None
        if not res.ok:
            raise self._fail(res, "read", path)
        return base64.b64decode(res.stdout)

    def write_bytes(self, path: str, data: bytes) -> None:
        q, tmp = shlex.quote(path), shlex.quote(f"{path}.tmp")
        d = shlex.quote(posixpath.dirname(path))
        res = self._exec(f"mkdir -p {d} && base64 -d > {tmp} && mv -f {tmp} {q}",
                         input=base64.b64encode(data).decode("ascii"), timeout=120)
        if not res.ok:
            raise self._fail(res, "write", path)

    def run(self, argv: list[str], cwd: str, input: str | None = None,
            timeout: float | None = None, on_output: OutputFn = None,
            on_start: StartFn = None, login: bool = False, env: dict | None = None) -> Result:
        prefix =["env", *(f"{k}={v}" for k, v in env.items())] if env else []
        script = f"cd {shlex.quote(cwd)} && " + " ".join(shlex.quote(a) for a in [*prefix, *argv])
        if login:
            script = self._login(script)
        return self._exec(script, input=input, timeout=timeout, on_output=on_output, on_start=on_start)

    def run_shell(self, command: str, cwd: str, timeout: float | None = None,
                  on_output: OutputFn = None, on_start: StartFn = None) -> Result:
        script = self._login(f"cd {shlex.quote(cwd)} && {command}")
        return self._exec(script, timeout=timeout, on_output=on_output, on_start=on_start)


    def shell_argv(self, path: str) -> list[str]:
        """ssh command for an interactive login shell in ``path`` (for a terminal window)."""
        script = f'cd {shlex.quote(path)} && exec "${{SHELL:-/bin/sh}}" -l'
        return ["ssh", "-t", *self._port_args(), self.target, "sh -c " + _pq(script)]

    def dir_status(self, path: str, timeout: float | None = None) -> Result:
        """Result whose exit code is 0 when ``path`` is a directory on the host."""
        return self._exec(f"test -d {shlex.quote(path)}", timeout=timeout)


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


# Printed before a script's own output: a PowerShell default shell loads the user's profile
# first, and anything the profile prints must not be mistaken for the script's output.
_PS_MARK = "##PATCHGOBLIN##"
_PS_PREAMBLE = ("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                f'[Console]::Out.Write("{_PS_MARK}`n"); ')
_PS_PREFIX = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
# A PowerShell default shell runs our line as `powershell -c <line>`, which turns the nested
# powershell's exit code into 0/1; this passes it on. cmd must not get it (it would reach
# powershell.exe as more base64).
_PS_EXIT_SUFFIX = "; exit $LASTEXITCODE"
_CMD_LIMIT = 8000  # cmd.exe caps command lines at 8191 characters


def _ps_command(script: str) -> str:
    """One remote command line that runs ``script`` under either default shell (see _PS_EXIT_SUFFIX)."""
    return _PS_PREFIX + base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _after_mark(text: str) -> str | None:
    """The text after the preamble's marker line, or None if the marker isn't in ``text``."""
    at = text.find(_PS_MARK)
    if at < 0:
        return None
    rest = text[at + len(_PS_MARK):]
    return rest[2:] if rest.startswith("\r\n") else rest.removeprefix("\n")


def _skip_to_mark(on_output: OutputFn) -> OutputFn:
    """Wrap a streaming callback so nothing printed before the marker (profile output) reaches it."""
    seen = False

    def forward(text: str) -> None:
        nonlocal seen
        if not seen:
            rest = _after_mark(text)
            if rest is None:
                return
            seen, text = True, rest
        if text:
            on_output(text)
    return forward


# Under a PowerShell default shell, command output passes through a second PowerShell that
# re-wraps and re-encodes long lines. Each output line is sent as ASCII base64 + "." instead.
_PS_FRAME = (" | ForEach-Object { [Console]::Out.WriteLine("
             "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$_)) + '.') }")


def _decode_frame(token: str) -> str:
    try:
        return base64.b64decode(token, validate=True).decode("utf-8", errors="replace") + "\n"
    except ValueError:
        return ""


class _Unframer:
    """Decodes the ``base64.`` frames of _PS_FRAME back into lines, ignoring any whitespace wrapping."""

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, text: str) -> str:
        self.buf += re.sub(r"\s+", "", text)
        *tokens, self.buf = self.buf.split(".")
        return "".join(_decode_frame(t) for t in tokens)

    def close(self) -> str:
        rest, self.buf = self.buf, ""
        return _decode_frame(rest) if rest else ""


def _unframe_stream(on_output: OutputFn) -> OutputFn:
    un = _Unframer()

    def forward(text: str) -> None:
        out = un.feed(text)
        if out:
            on_output(out)
    return forward


_WIN_ABS_RE = re.compile(r"^[A-Za-z]:\\")


class WindowsSSHHost(SSHHost):
    """A Windows host (OpenSSH Server, default shell cmd or PowerShell).

    Every command is a single ``powershell.exe -EncodedCommand`` line, so the server's
    default shell never parses user data. File contents travel as raw UTF-8 bytes.
    """

    os = "windows"
    lists_drives = True
    noop_script = "exit 0"

    def _exec(self, script: str, **kw) -> Result:
        command = _ps_command(_PS_PREAMBLE + script)
        if detect_windows_shell(self.target, self.port) != "cmd":
            command += _PS_EXIT_SUFFIX
        if len(command) > _CMD_LIMIT:
            raise HostError("Command is too long for a Windows SSH host "
                            "(pass the prompt on stdin instead of as an argument).")
        if kw.get("on_output"):
            kw["on_output"] = _skip_to_mark(kw["on_output"])
        res = communicate(self._argv(command), **kw)
        rest = _after_mark(res.stdout)
        if rest is not None:
            res.stdout = rest
        return res

    def _exec_retry(self, script: str, verb: str, path: str, **kw) -> Result:
        """``_exec`` once more after a HostError or a timeout (file reads/writes are repeatable)."""
        try:
            res = self._exec(script, **kw)
            if not res.timed_out:
                return res
        except HostError:
            pass
        try:
            return self._exec(script, **kw)
        except HostError as exc:
            raise HostError(f"Could not {verb} {path} on {self.label}: {exc}") from exc

    def _fail(self, res: Result, verb: str, path: str) -> HostError:
        if res.timed_out:
            return HostError(f"Could not {verb} {path} on {self.label}: timed out")
        return super()._fail(res, verb, path)

    def join(self, *parts: str) -> str:
        return ntpath.join(*parts)

    def normalize(self, path: str) -> str:
        path = path.strip().replace("/", "\\")
        if not _WIN_ABS_RE.match(path):
            raise HostError("Use an absolute Windows path on the remote host, e.g. C:\\Users\\me\\project.")
        return ntpath.normpath(path)

    def dir_status(self, path: str, timeout: float | None = None) -> Result:
        return self._exec(f"if (Test-Path -LiteralPath {_ps_quote(path)} -PathType Container) "
                          "{exit 0} else {exit 1}", timeout=timeout)

    def is_dir(self, path: str) -> bool:
        return self.dir_status(path, timeout=60).ok

    def ensure_dir(self, path: str) -> None:
        res = self._exec(f"New-Item -ItemType Directory -Force -Path {_ps_quote(path)} | Out-Null", timeout=60)
        if not res.ok:
            raise HostError(f"Could not create {path} on {self.label}: {res.stderr.strip()}")

    def home(self) -> str:
        res = self._exec("[Console]::Out.Write($env:USERPROFILE)", timeout=30)
        out = res.stdout.strip()
        if not res.ok or not _WIN_ABS_RE.match(out):
            raise HostError(f"Could not find the home directory on {self.label}: {res.stderr.strip()}")
        return out

    def list_dirs(self, path: str) -> dict:
        if not path:
            res = self._exec("Get-PSDrive -PSProvider FileSystem | ForEach-Object { $_.Root }", timeout=60)
            if not res.ok:
                raise self._fail(res, "list", "drives")
            drives = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
            return {"path": "", "parent": None, "sep": "\\",
                    "dirs": [{"name": d, "path": d} for d in drives]}
        path = self.normalize(path)
        q = _ps_quote(path)
        script = (f"if (-not (Test-Path -LiteralPath {q} -PathType Container)) {{exit 45}}; "
                  f"(Resolve-Path -LiteralPath {q}).ProviderPath; "
                  f"Get-ChildItem -LiteralPath {q} -Directory -Force -ErrorAction SilentlyContinue "
                  "| ForEach-Object { $_.Name }; exit 0")
        res = self._exec(script, timeout=60)
        if res.returncode == 45:
            raise HostError(f"Not a directory (or no access): {path}")
        if not res.ok:
            raise self._fail(res, "list", path)
        lines = [ln.rstrip("\r") for ln in res.stdout.splitlines()]
        cwd = lines[0] if lines and lines[0] else path
        names = sorted((n for n in lines[1:] if n), key=str.lower)
        parent = ntpath.dirname(cwd)
        if parent == cwd or not parent:  # drive root: back to the drive list
            parent = ""
        return {"path": cwd, "parent": parent, "sep": "\\",
                "dirs": [{"name": n, "path": ntpath.join(cwd, n)} for n in names]}

    def read_text(self, path: str) -> str | None:
        q = _ps_quote(path)
        # A PowerShell default shell re-decodes the raw stream with its own code page, so
        # there the contents travel as base64 (as in read_bytes); cmd keeps the raw bytes.
        b64 = detect_windows_shell(self.target, self.port) != "cmd"
        head = f"if (-not (Test-Path -LiteralPath {q} -PathType Leaf)) {{exit 44}}; "
        if b64:
            script = head + f"[Console]::Out.Write([Convert]::ToBase64String([IO.File]::ReadAllBytes({q})))"
        else:
            script = (head + f"$b=[IO.File]::ReadAllBytes({q}); $o=[Console]::OpenStandardOutput(); "
                      "$o.Write($b,0,$b.Length); $o.Flush()")
        res = self._exec_retry(script, "read", path, timeout=120)
        if res.returncode == 44:
            return None
        if not res.ok:
            raise self._fail(res, "read", path)
        if b64:
            return base64.b64decode(res.stdout.strip()).decode("utf-8")
        return res.stdout

    def list_dir(self, path: str) -> list[str]:
        q = _ps_quote(path)
        res = self._exec(f"if (Test-Path -LiteralPath {q} -PathType Container) "
                         f"{{Get-ChildItem -LiteralPath {q} -Force -Name}}", timeout=60)
        if not res.ok:
            raise self._fail(res, "list", path)
        return sorted(line.rstrip("\r") for line in res.stdout.splitlines() if line.strip())

    def write_text(self, path: str, text: str) -> None:
        q, tmp = _ps_quote(path), _ps_quote(f"{path}.tmp")
        script = (f"$d=Split-Path -Parent {q}; "
                  "if ($d) { New-Item -ItemType Directory -Force -Path $d | Out-Null }; "
                  "$m=New-Object IO.MemoryStream; [Console]::OpenStandardInput().CopyTo($m); "
                  f"[IO.File]::WriteAllBytes({tmp}, $m.ToArray()); "
                  f"Move-Item -Force -LiteralPath {tmp} -Destination {q}")
        res = self._exec_retry(script, "write", path, input=text, timeout=120)
        if not res.ok:
            raise self._fail(res, "write", path)

    def read_bytes(self, path: str) -> bytes | None:
        q = _ps_quote(path)
        script = (f"if (-not (Test-Path -LiteralPath {q} -PathType Leaf)) {{exit 44}}; "
                  f"[Console]::Out.Write([Convert]::ToBase64String([IO.File]::ReadAllBytes({q})))")
        res = self._exec(script, timeout=120)
        if res.returncode == 44:
            return None
        if not res.ok:
            raise self._fail(res, "read", path)
        return base64.b64decode(res.stdout.strip())

    def write_bytes(self, path: str, data: bytes) -> None:
        q, tmp = _ps_quote(path), _ps_quote(f"{path}.tmp")
        script = (f"$d=Split-Path -Parent {q}; "
                  "if ($d) { New-Item -ItemType Directory -Force -Path $d | Out-Null }; "
                  "$t=[Console]::In.ReadToEnd(); "
                  f"[IO.File]::WriteAllBytes({tmp}, [Convert]::FromBase64String($t.Trim())); "
                  f"Move-Item -Force -LiteralPath {tmp} -Destination {q}")
        res = self._exec(script, input=base64.b64encode(data).decode("ascii"), timeout=120)
        if not res.ok:
            raise self._fail(res, "write", path)

    def run(self, argv: list[str], cwd: str, input: str | None = None,
            timeout: float | None = None, on_output: OutputFn = None,
            on_start: StartFn = None, login: bool = False, env: dict | None = None) -> Result:
        # ``login`` is a no-op: Windows sshd sessions already carry the user's PATH.
        sets = "".join(f"$env:{k}={_ps_quote(v)}; " for k, v in (env or {}).items())
        call = "& " + " ".join(_ps_quote(a) for a in argv)
        framed = detect_windows_shell(self.target, self.port) != "cmd"
        if framed:
            call += _PS_FRAME
            if on_output:
                on_output = _unframe_stream(on_output)
        script = f"{sets}Set-Location -LiteralPath {_ps_quote(cwd)}; {call}; exit $LASTEXITCODE"
        res = self._exec(script, input=input, timeout=timeout, on_output=on_output, on_start=on_start)
        if framed:
            un = _Unframer()
            res.stdout = un.feed(res.stdout) + un.close()
        return res

    def run_shell(self, command: str, cwd: str, timeout: float | None = None,
                  on_output: OutputFn = None, on_start: StartFn = None) -> Result:
        # Unframed on purpose: arbitrary PowerShell keeps its own output formatting.
        script = (f"Set-Location -LiteralPath {_ps_quote(cwd)}; {command}\n"
                  "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }")
        return self._exec(script, timeout=timeout, on_output=on_output, on_start=on_start)

    def shell_argv(self, path: str) -> list[str]:
        script = f"Set-Location -LiteralPath {_ps_quote(path)}"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return ["ssh", "-t", *self._port_args(), self.target, f"powershell.exe -NoLogo -NoExit -EncodedCommand {encoded}"]


_SSH_OS: dict[tuple, str] = {}
_SSH_OS_LOCK = threading.Lock()


def detect_ssh_os(target: str, port: int | None = None) -> str:
    """``"posix"`` or ``"windows"`` for an SSH target; successes are cached, failures are not."""
    key = (target, port or None)
    with _SSH_OS_LOCK:
        if key in _SSH_OS:
            return _SSH_OS[key]
    probe_host = SSHHost(target, port)
    res = communicate(probe_host._argv("uname -s"), timeout=30)
    if res.timed_out or res.returncode == 255:
        raise HostError(f"SSH connection to {probe_host.label} failed: "
                        f"{'timed out' if res.timed_out else res.stderr.strip() or res.returncode}")
    found = None
    # Git for Windows / MSYS put a uname on PATH that reports e.g. MINGW64_NT-10.0.
    if res.ok and not re.match(r"\s*(MINGW|MSYS|CYGWIN)", res.stdout, re.IGNORECASE):
        found = "posix"
    else:
        res = communicate(probe_host._argv(_ps_command("[Environment]::OSVersion.Platform")), timeout=30)
        if res.ok and "Win32NT" in res.stdout:
            found = "windows"
    if not found:
        raise HostError(f"Could not detect the remote OS on {probe_host.label}.")
    with _SSH_OS_LOCK:
        _SSH_OS[key] = found
    return found


_WIN_SHELL: dict[tuple, str] = {}


def detect_windows_shell(target: str, port: int | None = None) -> str:
    """``"cmd"`` or ``"powershell"``: the Windows OpenSSH server's DefaultShell (anything that
    isn't cmd is treated like PowerShell). Successes are cached, failures are not."""
    key = (target, port or None)
    with _SSH_OS_LOCK:
        if key in _WIN_SHELL:
            return _WIN_SHELL[key]
    probe_host = SSHHost(target, port)
    # cmd expands %OS% to Windows_NT; PowerShell prints it literally.
    res = communicate(probe_host._argv("echo %OS%"), timeout=30)
    if res.timed_out or res.returncode == 255:
        raise HostError(f"SSH connection to {probe_host.label} failed: "
                        f"{'timed out' if res.timed_out else res.stderr.strip() or res.returncode}")
    found = "cmd" if "Windows_NT" in res.stdout else "powershell"
    with _SSH_OS_LOCK:
        _WIN_SHELL[key] = found
    return found


_LINUX_TERMINALS = (
    # (binary, flags before the command to run inside it)
    ("terminalgoblin", ["-e"]),  # with a command, opens a tab in the running TerminalGoblin
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("xterm", ["-e"]),
)


# Windows Terminal window that every PatchGoblin terminal opens a tab in (created on first use).
TERMINAL_WINDOW = "PatchGoblin"


def terminal_color(index: int) -> str:
    """A tab colour for the project at ``index`` in the project list: hues a golden angle
    apart, so neighbouring projects get clearly different colours and each keeps its own."""
    r, g, b = colorsys.hls_to_rgb((index * 137.508 % 360) / 360, 0.45, 0.65)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


# Each project terminal tab's command line carries "PATCHGOBLIN_TAB<sep><project id>", so the
# tabs that are still open can be found among the running processes (see wt_tabs).
WT_TAG = "PATCHGOBLIN_TAB"
_WT_TAG_RE = re.compile(WT_TAG + r"\W{1,3}([A-Za-z0-9_-]+)")
_TAG_OK = re.compile(r"^[A-Za-z0-9_-]+$")


def windows_shell(which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """PowerShell 7 when installed, else Windows PowerShell."""
    return ["pwsh.exe", "-NoLogo"] if which("pwsh") else ["powershell.exe", "-NoLogo"]


def _wrap_terminal(inner: list[str] | None, cwd_path: str | None, platform: str,
                   which: Callable[[str], str | None], title: str = "",
                   color: str = "", tag: str = "") -> tuple[list[str], str | None]:
    """Argv and cwd opening a terminal on this computer that runs ``inner`` (or a shell in
    ``cwd_path`` when ``inner`` is None), named ``title`` and with tab colour ``color``
    (#rrggbb) where the terminal allows it. In Windows Terminal a local shell is tagged with
    ``tag`` (callers tag ``inner`` themselves)."""
    cwd = cwd_path if inner is None else None
    title = " ".join(title.split())  # one line
    if platform == "win32":
        if which("wt"):
            # A new tab in the one named window, not a new window each time. The title is kept
            # even when the shell or a remote session sets its own. Windows Terminal treats ";"
            # as a command separator, so escape it.
            esc = lambda a: a.replace(";", "\\;")
            argv = ["wt", "-w", TERMINAL_WINDOW, "new-tab"]
            if title:
                argv += ["--title", esc(title), "--suppressApplicationTitle"]
            if re.fullmatch(r"#[0-9a-fA-F]{6}", color or ""):
                argv += ["--tabColor", color]
            if inner is None and tag and _TAG_OK.match(tag):
                shell = [*windows_shell(which), "-NoExit", "-Command", f"${{env:{WT_TAG}}}='{tag}'"]
                return [*argv, "-d", esc(cwd_path), *shell], None
            if inner is None:
                return [*argv, "-d", esc(cwd_path)], None
            return [*argv, *[esc(a) for a in inner]], None
        if inner is None and title:
            return ["powershell.exe", "-NoExit", "-Command", f"$Host.UI.RawUI.WindowTitle = {_ps_quote(title)}"], cwd
        return inner or ["powershell.exe", "-NoExit"], cwd
    if platform == "darwin":
        if inner is None:
            return ["open", "-a", "Terminal", cwd_path], None
        script = shlex.join(inner).replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                "-e", 'tell application "Terminal" to activate'], None
    for name, flags in _LINUX_TERMINALS:
        if not which(name):
            continue
        if name == "terminalgoblin":
            # This opens a tab in the running TerminalGoblin, which doesn't inherit our cwd: pass it.
            argv = [name] + (["--title", title] if title else [])
            if re.fullmatch(r"#[0-9a-fA-F]{6}", color or ""):
                argv += ["--color", color]
            return (argv + [*flags, *inner], None) if inner else (argv + ["--cwd", cwd_path], None)
        return [name, *(flags + inner if inner else [])], cwd
    raise HostError("No terminal emulator found (tried " + ", ".join(n for n, _ in _LINUX_TERMINALS) + ").")


def terminal_command(host, path: str, platform: str = sys.platform,
                     which: Callable[[str], str | None] = shutil.which, title: str = "",
                     color: str = "", tag: str = "") -> tuple[list[str], str | None]:
    """Argv and working directory that open a terminal on this computer (a tab of the
    PatchGoblin window in Windows Terminal, tagged with ``tag``: see wt_tabs).

    Local projects get a shell in ``path``; SSH projects get a local terminal running
    ssh that lands in ``path`` on the remote host.
    """
    inner = None
    if host.kind == "ssh":
        inner = host.shell_argv(path)
        if tag and _TAG_OK.match(tag) and platform == "win32":
            # Visible in the local ssh.exe command line; servers that don't AcceptEnv ignore it.
            inner = [inner[0], "-o", f"SetEnv={WT_TAG}={tag}", *inner[1:]]
    return _wrap_terminal(inner, path, platform, which, title, color, tag)


def terminal_title(project: dict) -> str:
    """A terminal's name for a project: its name, plus the host for SSH projects."""
    path = project.get("path") or ""
    title = project.get("name") or os.path.basename(path.rstrip("/\\")) or path
    return f"{title} ({project.get('ssh_target')})" if project.get("location") == "ssh" else title


def parse_wt_tabs(text: str) -> list[str]:
    """Project ids from a JSON list of command lines (oldest first), skipping the query itself."""
    try:
        lines = json.loads(text or "[]")
    except ValueError:
        return []
    out = []
    for line in lines if isinstance(lines, list) else [lines]:
        if isinstance(line, str) and "Get-CimInstance" not in line:
            m = _WT_TAG_RE.search(line)
            if m:
                out.append(m.group(1))
    return out


def wt_tabs() -> list[str]:
    """The project ids of PatchGoblin's terminal tabs that are still open, in the order they
    were opened (which is their tab order unless tabs were moved by hand). Windows only;
    empty when anything goes wrong."""
    script = ("$p = Get-CimInstance Win32_Process -Filter \"CommandLine LIKE '%" + WT_TAG + "%'\" | "
              "Where-Object { $_.Name -match '^(pwsh|powershell|ssh)\\.exe$' } | Sort-Object CreationDate; "
              "ConvertTo-Json -Compress -InputObject @($p | ForEach-Object { $_.CommandLine })")
    try:
        result = communicate(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout=20)
    except Exception:
        return []
    return parse_wt_tabs(result.stdout) if result.returncode == 0 else []


def run_in_terminal(argv: list[str], platform: str = sys.platform,
                    which: Callable[[str], str | None] = shutil.which, title: str = "") -> None:
    """Open a terminal on this computer running ``argv`` (e.g. ssh asking for a password)."""
    cmd, cwd = _wrap_terminal(argv, None, platform, which, title)
    try:
        _spawn(cmd, cwd, "CREATE_NEW_CONSOLE")
    except OSError as exc:
        raise HostError(f"Could not open a terminal: {exc}") from exc


def _local_dir_guard(project: dict) -> tuple:
    """``(host, path)`` for a project about to be opened in a local window; a local project's
    directory must exist."""
    host = host_for(project)
    path = project["path"]
    if host.kind == "local" and not host.is_dir(path):
        raise HostError(f"Directory does not exist: {path}")
    return host, path


def open_terminal(project: dict, color: str = "", new: bool = False, platform: str = sys.platform,
                  terminal_settings: dict | None = None) -> str:
    """Open a terminal on this computer in the project's directory, named after the project
    and with tab colour ``color`` (see terminal_color). In Windows Terminal, or on Linux with
    a tab backend (``terminal_settings``, see linux_terminals), the project's tab is focused
    if it's still open (unless ``new``). Returns "focused" or "opened"."""
    host, path = _local_dir_guard(project)
    tag = project.get("id") or ""
    if platform.startswith("linux") and terminal_settings is not None:
        from . import linux_terminals
        backend = linux_terminals.resolve_backend(terminal_settings)
        if backend:
            return _open_in_backend(backend, host, path, tag, terminal_title(project), color, new)
    # One look-up-then-open at a time, and a tab opened moments ago counts as open even before its
    # shell shows up in the process list, so a double-click doesn't open two tabs.
    with _opening_lock:
        try:
            if not new and tag and platform == "win32" and shutil.which("wt"):
                if time.monotonic() - _just_opened.get(tag, -OPEN_GRACE) < OPEN_GRACE:
                    return "focused"
                tabs = wt_tabs()
                if tag in tabs:
                    _spawn(["wt", "-w", TERMINAL_WINDOW, "focus-tab", "--target", str(tabs.index(tag))], None,
                           "CREATE_NEW_CONSOLE")
                    return "focused"
            argv, cwd = terminal_command(host, path, platform, title=terminal_title(project), color=color, tag=tag)
            _spawn(argv, cwd, "CREATE_NEW_CONSOLE")
        except OSError as exc:
            raise HostError(f"Could not open a terminal: {exc}") from exc
        if tag:
            _just_opened[tag] = time.monotonic()
    return "opened"


def _open_in_backend(backend, host, path: str, tag: str, title: str, color: str, new: bool) -> str:
    """open_terminal for a Linux tab backend: focus the project's tab if it's open, else open one."""
    tag = tag if _TAG_OK.match(tag) else ""
    with _opening_lock:
        try:
            if not new and tag:
                if time.monotonic() - _just_opened.get(tag, -OPEN_GRACE) < OPEN_GRACE:
                    return "focused"
                if tag in backend.list_tags():
                    backend.focus(tag)
                    return "focused"
            backend.open(host.shell_argv(path) if host.kind == "ssh" else None, path, title, color, tag)
        except OSError as exc:
            raise HostError(f"Could not open a terminal: {exc}") from exc
        if tag:
            _just_opened[tag] = time.monotonic()
    return "opened"


OPEN_GRACE = 3.0  # seconds a just-opened tab counts as open
_opening_lock = threading.Lock()
_just_opened: dict[str, float] = {}


def _spawn(argv: list[str], cwd: str | None, windows_flag: str) -> None:
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
                   which: Callable[[str], str | None] = shutil.which) -> list[str]:
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
    host, path = _local_dir_guard(project)
    argv = vscode_command(host, path)
    try:
        _spawn(argv, None, "CREATE_NO_WINDOW")
    except OSError as exc:
        raise HostError(f"Could not open VS Code: {exc}") from exc


def host_for(project: dict):
    if project.get("location") == "ssh":
        target, port = project.get("ssh_target", ""), project.get("ssh_port")
        remote_os = project.get("ssh_os") or detect_ssh_os(target, port)
        return (WindowsSSHHost if remote_os == "windows" else SSHHost)(target, port)
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
        res = host.dir_status(path, timeout=20)
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
