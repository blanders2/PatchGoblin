"""Pseudo-terminal sessions for the browser terminals: a shell in a PTY plus its scrollback.

Windows uses ConPTY through pywinpty; elsewhere the standard ``pty`` module. A reader thread
copies output into a bounded scrollback whose characters are numbered from the start of the
session, so a browser can (re)attach from any offset and only get what it hasn't seen.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import uuid

SCROLLBACK = 1_000_000  # characters kept per session
MAX_SESSIONS = 12  # live (running) sessions at once


class _WinPty:
    def __init__(self, argv, cwd, env, cols, rows):
        from winpty import Backend, PtyProcess
        exe = shutil.which(argv[0]) or argv[0]
        # pywinpty's ConPTY backend (its default) doesn't turn ^C into an interrupt, so Ctrl+C
        # couldn't stop a running command; its WinPTY backend does. ConPTY is the fallback.
        try:
            self.proc = PtyProcess.spawn([exe, *argv[1:]], cwd=cwd, env=env, dimensions=(rows, cols),
                                         backend=Backend.WinPTY)
        except Exception:
            self.proc = PtyProcess.spawn([exe, *argv[1:]], cwd=cwd, env=env, dimensions=(rows, cols))

    def read(self) -> str:
        return self.proc.read(65536)  # blocks; EOFError when the shell is gone

    def write(self, data: str) -> None:
        self.proc.write(data)

    def resize(self, cols: int, rows: int) -> None:
        self.proc.setwinsize(rows, cols)

    def alive(self) -> bool:
        return self.proc.isalive()

    def exit_code(self):
        return self.proc.exitstatus

    def kill(self) -> None:
        try:
            self.proc.terminate(force=True)
        finally:
            self.close()

    def close(self) -> None:
        try:
            self.proc.close()
        except Exception:
            pass


class _PosixPty:
    def __init__(self, argv, cwd, env, cols, rows):
        import codecs
        import pty
        self.master, slave = pty.openpty()
        self._size(slave, cols, rows)
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                     start_new_session=True, close_fds=True)
        os.close(slave)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    @staticmethod
    def _size(fd, cols, rows):
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def read(self) -> str:
        try:
            data = os.read(self.master, 65536)
        except OSError:  # EIO once the child side is closed
            data = b""
        if not data:
            raise EOFError
        return self.decoder.decode(data)

    def write(self, data: str) -> None:
        os.write(self.master, data.encode("utf-8"))

    def resize(self, cols: int, rows: int) -> None:
        self._size(self.master, cols, rows)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def exit_code(self):
        return self.proc.poll()

    def kill(self) -> None:
        import signal
        try:
            os.killpg(self.proc.pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.close()

    def close(self) -> None:
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None


def spawn_pty(argv, cwd, env, cols, rows):
    return (_WinPty if os.name == "nt" else _PosixPty)(argv, cwd, env, cols, rows)


def clamp_size(cols, rows) -> tuple[int, int]:
    try:
        return max(10, min(int(cols), 500)), max(4, min(int(rows), 300))
    except (TypeError, ValueError):
        raise ValueError("cols and rows must be numbers.") from None


class Session:
    def __init__(self, argv: list[str], cwd: str | None, title: str, color: str = "", project: str = "",
                 cols: int = 100, rows: int = 30, notify: threading.Condition | None = None):
        self.notify = notify  # the manager's shared condition, woken on any change
        self.id = uuid.uuid4().hex[:10]
        self.argv, self.cwd, self.title, self.color, self.project = argv, cwd, title, color, project
        self.created = time.time()
        self.cols, self.rows = clamp_size(cols, rows)
        self.buf = ""  # the scrollback: output characters [start, end)
        self.start = 0
        self.cond = threading.Condition()
        self.exit_code = None
        self.running = True
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        env.pop("PATCHGOBLIN_TAB", None)
        self.pty = spawn_pty(argv, cwd, env, self.cols, self.rows)
        threading.Thread(target=self._pump, name=f"pty-{self.id}", daemon=True).start()

    @property
    def end(self) -> int:
        return self.start + len(self.buf)

    def _pump(self) -> None:
        while True:
            try:
                data = self.pty.read()
            except (EOFError, OSError):
                break
            if data:
                self._append(data)
        for _ in range(20):  # the exit code can lag the end of output a little
            if not self.pty.alive():
                break
            time.sleep(0.05)
        with self.cond:
            self.exit_code = self.pty.exit_code()
            self.running = False
            self.cond.notify_all()
        self._notify()
        self.pty.close()

    def _append(self, data: str) -> None:
        """Add output, dropping the oldest beyond SCROLLBACK (offsets keep counting)."""
        with self.cond:
            self.buf += data
            if len(self.buf) > SCROLLBACK:
                cut = len(self.buf) - SCROLLBACK
                self.buf = self.buf[cut:]
                self.start += cut
            self.cond.notify_all()
        self._notify()

    def _notify(self) -> None:
        """Wake multiplexed streams; called outside ``self.cond`` so the lock order stays simple."""
        if self.notify is not None:
            with self.notify:
                self.notify.notify_all()

    def read_since(self, since: int, wait: float = 0) -> tuple[str, int, bool]:
        """``(text, next offset, truncated)``: output after ``since`` (waiting up to ``wait`` s
        for some). ``truncated`` means older output already left the scrollback."""
        with self.cond:
            if wait and self.running and since >= self.end:
                self.cond.wait(wait)
            truncated = since < self.start
            begin = max(since, self.start) - self.start
            return self.buf[begin:], self.end, truncated

    def write(self, data: str) -> None:
        if not self.running:
            raise ValueError("This terminal has exited.")
        self.pty.write(data)

    def resize(self, cols, rows) -> None:
        self.cols, self.rows = clamp_size(cols, rows)
        if self.running:
            self.pty.resize(self.cols, self.rows)

    def kill(self) -> None:
        if self.running:
            try:
                self.pty.kill()
            except Exception:
                pass
        with self.cond:
            self.running = False
            self.cond.notify_all()
        self._notify()

    def view(self) -> dict:
        return {"id": self.id, "title": self.title, "color": self.color, "project": self.project,
                "alive": self.running, "exit_code": self.exit_code, "created": self.created,
                "cols": self.cols, "rows": self.rows}


class SessionManager:
    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self.changed = threading.Condition()  # notified when any session gets output or stops

    def wait_any(self, offsets: dict[str, int], timeout: float, stop=None) -> None:
        """Return once any listed session has output past its offset or has stopped, ``stop()``
        is true (or after ``timeout`` s). Wakeups may be spurious; callers re-check."""
        def ready() -> bool:
            if stop is not None and stop():
                return True
            for sid, offset in offsets.items():
                s = self.get(sid)
                if s is None or not s.running or s.end > offset:
                    return True
            return False
        with self.changed:
            self.changed.wait_for(ready, timeout)

    def create(self, argv, cwd, title, color="", project="", cols=100, rows=30) -> Session:
        with self.lock:
            if sum(s.running for s in self.sessions.values()) >= MAX_SESSIONS:
                raise ValueError(f"At most {MAX_SESSIONS} terminals can run at once. Close one first.")
        session = Session(argv, cwd, title, color, project, cols, rows, notify=self.changed)
        with self.lock:
            self.sessions[session.id] = session
        return session

    def get(self, sid: str) -> Session | None:
        with self.lock:
            return self.sessions.get(sid)

    def list(self) -> list[Session]:
        with self.lock:
            return sorted(self.sessions.values(), key=lambda s: s.created)

    def close(self, sid: str) -> bool:
        with self.lock:
            session = self.sessions.pop(sid, None)
        if session:
            session.kill()
        return session is not None

    def kill_all(self) -> None:
        with self.lock:
            sessions, self.sessions = list(self.sessions.values()), {}
        for s in sessions:
            s.kill()


def local_shell(platform: str = sys.platform) -> list[str]:
    """The interactive shell for a local project terminal."""
    if platform == "win32":
        return ["pwsh.exe", "-NoLogo"] if shutil.which("pwsh") else ["powershell.exe", "-NoLogo"]
    return [os.environ.get("SHELL") or "/bin/sh", "-l"]
