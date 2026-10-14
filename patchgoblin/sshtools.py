"""SSH tools: public keys (list, generate, copy to hosts) and port-forwarding tunnels.

Only public keys are ever read, returned or copied; a private key file is never opened.
Remote work goes through the same ``SSHHost``/``WindowsSSHHost`` layer as projects, so
quoting, encodings and the Windows default-shell quirks are handled in one place. Tunnels
are plain ``ssh -N -L/-R`` processes that stop when PatchGoblin exits.
"""
from __future__ import annotations

import base64
import os
import re
import shlex
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import subprocess

from .hosts import (HostError, SSHHost, WindowsSSHHost, _CMD_LIMIT, _ps_quote, background_kwargs,
                    communicate, kill_tree, run_in_terminal)

KEY_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PUBKEY_RE = re.compile(r"^(ssh-|ecdsa-|sk-)\S+ [A-Za-z0-9+/=]+( [^\r\n]*)?$")
KEY_TYPES = ("ed25519", "rsa", "ecdsa")
TUNNEL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEST_HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]_]+$")
BINDS = ("127.0.0.1", "0.0.0.0")
DIRECTIONS = ("local", "remote")
STARTUP_WAIT = 2.0  # seconds a new tunnel must stay alive to count as started


class AuthError(HostError):
    """ssh could not log in (exit 255): the host needs working key login."""


def ssh_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".ssh")


# ---- validation ----------------------------------------------------------------------

def valid_key_name(name) -> str:
    if not isinstance(name, str) or not KEY_NAME.match(name) or name[0] in "-.":
        raise ValueError("Key names use letters, digits, '.', '_' and '-' (up to 64), "
                         "and cannot start with '-' or '.'.")
    return name


def clean_pubkey(text) -> str:
    """One public key line, or ValueError. Private keys are refused outright."""
    if not isinstance(text, str):
        raise ValueError("A public key must be text.")
    if "-----BEGIN" in text or "PRIVATE KEY" in text:
        raise ValueError("That looks like a private key. Only public keys can be copied.")
    line = text.strip()
    if "\n" in line or "\r" in line:
        raise ValueError("Each public key must be a single line.")
    if not PUBKEY_RE.match(line):
        raise ValueError("Not a public key line (expected e.g. 'ssh-ed25519 AAAA… comment').")
    return line


def _key_dict(name: str, path: str, text: str, has_private: bool, fingerprint: str) -> dict:
    parts = text.split(" ", 2)
    return {"name": name, "path": path, "type": parts[0], "comment": parts[2] if len(parts) > 2 else "",
            "fingerprint": fingerprint, "has_private": has_private, "text": text}


def _fingerprint(args: list[str], input: Optional[str] = None) -> str:
    res = communicate(["ssh-keygen", "-lf", *args], input=input, timeout=15)
    parts = res.stdout.split()
    return parts[1] if res.ok and len(parts) > 1 else ""


# ---- keys ----------------------------------------------------------------------------

def list_local_keys() -> list[dict]:
    """Public keys in ~/.ssh. Only ``*.pub`` files are opened."""
    folder, out = ssh_dir(), []
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".pub"))
    except OSError:
        return []
    for name in names:
        path = os.path.join(folder, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not PUBKEY_RE.match(text):
            continue
        stem = name[:-4]
        out.append(_key_dict(stem, path, text, os.path.isfile(os.path.join(folder, stem)),
                             _fingerprint([path])))
    return out


def list_remote_keys(host) -> list[dict]:
    """Public keys in the host's ~/.ssh. Only ``*.pub`` files are read; whether a private key
    exists is judged from the directory listing alone."""
    folder = host.join(host.home(), ".ssh")
    names = host.list_dir(folder)
    out = []
    for name in names:
        if not name.endswith(".pub"):
            continue
        path = host.join(folder, name)
        text = (host.read_text(path) or "").strip()
        if not PUBKEY_RE.match(text):
            continue
        stem = name[:-4]
        out.append(_key_dict(stem, path, text, stem in names, _fingerprint(["-"], input=text + "\n")))
    return out


def generate_key(name: str, key_type: str = "ed25519", comment: str = "", passphrase: str = "",
                 host=None) -> dict:
    """Create a key pair in ~/.ssh (here, or on ``host``) and return its public half."""
    valid_key_name(name)
    if key_type not in KEY_TYPES:
        raise ValueError(f"Key type must be one of {', '.join(KEY_TYPES)}.")
    for field_name, value in (("comment", comment), ("passphrase", passphrase)):
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise ValueError(f"The {field_name} must be a single line.")
    folder = ssh_dir() if host is None else host.join(host.home(), ".ssh")
    join = os.path.join if host is None else host.join
    path = join(folder, name)
    if host is None:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        existing = os.listdir(folder)
    else:
        host.ensure_dir(folder)
        existing = host.list_dir(folder)
    if name in existing or f"{name}.pub" in existing:
        raise ValueError(f"A key named {name!r} already exists in {folder}.")
    argv = ["ssh-keygen", "-t", key_type, *(["-b", "4096"] if key_type == "rsa" else []),
            "-f", path, "-C", comment, "-N", passphrase, "-q"]
    if host is None:
        res = communicate(argv, timeout=120)
    else:
        res = host.run(argv, cwd=host.home(), timeout=120)
    if not res.ok:
        raise HostError(f"ssh-keygen failed: {res.stderr.strip() or res.returncode}")
    if host is None:
        with open(f"{path}.pub", encoding="utf-8") as fh:
            text = fh.read().strip()
        return _key_dict(name, f"{path}.pub", text, True, _fingerprint([f"{path}.pub"]))
    text = (host.read_text(f"{path}.pub") or "").strip()
    return _key_dict(name, f"{path}.pub", text, True, _fingerprint(["-"], input=text + "\n"))


# ---- installing keys -----------------------------------------------------------------

_POSIX_INSTALL = (
    "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
    'while IFS= read -r k; do [ -n "$k" ] || continue; '
    'grep -qxF "$k" ~/.ssh/authorized_keys && echo "SKIP $k" || '
    '{ printf \'%s\\n\' "$k" >> ~/.ssh/authorized_keys; echo "ADD $k"; }; done; '
    "chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys")

# PowerShell: expects $keys (an array of lines) to be defined before it.
_PS_INSTALL_BODY = (
    "$d=Join-Path $env:USERPROFILE '.ssh'; New-Item -ItemType Directory -Force -Path $d | Out-Null; "
    "$f=Join-Path $d 'authorized_keys'; "
    "$text=''; if (Test-Path -LiteralPath $f) { $text=[IO.File]::ReadAllText($f) }; "
    "$have=@($text -split \"`r?`n\" | ForEach-Object { $_.Trim() }); $new=@(); "
    "foreach ($k in $keys) { "
    "$b=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($k)); "
    "if ($have -contains $k) { \"SKIP $b\" } else { \"ADD $b\"; $new+=$k; $have+=$k } }; "
    "if ($new.Count) { if ($text -and -not $text.EndsWith(\"`n\")) { $text+=\"`n\" }; "
    "$text+=(($new -join \"`n\")+\"`n\"); "
    "[IO.File]::WriteAllText($f,$text,[Text.UTF8Encoding]::new($false)) }; "
    "if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
    ".IsInRole('Administrators')) { 'ADMIN' }")

_PS_STDIN_KEYS = (
    "$m=New-Object IO.MemoryStream; [Console]::OpenStandardInput().CopyTo($m); "
    "$keys=@([Text.Encoding]::UTF8.GetString($m.ToArray()) -split \"`n\" | "
    "ForEach-Object { $_.Trim() } | Where-Object { $_ }); ")

ADMIN_NOTE = ("This Windows account is an Administrator: sshd ignores ~/.ssh/authorized_keys for "
              "administrators and reads C:\\ProgramData\\ssh\\administrators_authorized_keys instead. "
              "PatchGoblin does not write there; add the key to that file yourself if key login still fails.")


def clean_pubkeys(pubkeys) -> list[str]:
    if not isinstance(pubkeys, (list, tuple)) or not pubkeys:
        raise ValueError("Choose at least one public key to copy.")
    keys = []
    for text in pubkeys:
        line = clean_pubkey(text)
        if line not in keys:
            keys.append(line)
    return keys


def install_keys(host, pubkeys) -> dict:
    """Add public keys to the host's authorized_keys (needs working key login).

    Returns ``{"added": [...], "skipped": [...], "note": str}`` of key lines.
    """
    keys = clean_pubkeys(pubkeys)
    stdin = "\n".join(keys) + "\n"
    windows = isinstance(host, WindowsSSHHost)
    res = host._exec(_PS_STDIN_KEYS + _PS_INSTALL_BODY if windows else _POSIX_INSTALL,
                     input=stdin, timeout=60)
    if res.timed_out:
        raise HostError(f"Timed out talking to {host.label}.")
    if res.returncode == 255:
        raise AuthError(f"SSH login to {host.label} failed: {res.stderr.strip() or 'no key login'}")
    if not res.ok:
        raise HostError(f"Could not update authorized_keys on {host.label}: "
                        f"{res.stderr.strip() or res.returncode}")
    added, skipped, note = [], [], ""
    for line in res.stdout.splitlines():
        verb, _, rest = line.strip().partition(" ")
        if verb == "ADMIN":
            note = ADMIN_NOTE
        elif verb in ("ADD", "SKIP"):
            if windows:
                try:
                    rest = base64.b64decode(rest).decode("utf-8")
                except ValueError:
                    continue
            (added if verb == "ADD" else skipped).append(rest)
    return {"added": added, "skipped": skipped, "note": note}


def install_keys_interactive(target: str, port: Optional[int], pubkeys, windows: bool = False) -> dict:
    """Password bootstrap: open a terminal window running ssh, where the user types the password."""
    keys = clean_pubkeys(pubkeys)
    SSHHost(target, port)  # validates the target
    if windows:
        script = "$keys=@(" + ",".join(_ps_quote(k) for k in keys) + "); " + _PS_INSTALL_BODY
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        remote = f"powershell -NoProfile -EncodedCommand {encoded}"
    else:
        remote = ("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; " + "; ".join(
            f"grep -qxF {shlex.quote(k)} ~/.ssh/authorized_keys || printf '%s\\n' {shlex.quote(k)} "
            ">> ~/.ssh/authorized_keys" for k in keys) +
            "; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; echo 'Keys installed.'")
    argv = ["ssh", "-t", *(["-p", str(int(port))] if port else []), target, remote]
    if sum(len(a) + 1 for a in argv) > _CMD_LIMIT:
        raise ValueError("Too many keys, install fewer at once.")
    run_in_terminal(argv, sys.platform)
    return {"added": [], "skipped": [], "note":
            "A terminal window opened: enter the password there. Run the install again with key login "
            "to check the result."}


# ---- targets -------------------------------------------------------------------------

def ssh_targets(registry) -> list[dict]:
    """SSH targets worth suggesting: those of saved projects plus ~/.ssh/config Host aliases."""
    out, seen = [], set()

    def add(target, port):
        if target and (target, port) not in seen:
            seen.add((target, port))
            out.append({"target": target, "port": port})

    for project in registry.list():
        if project.get("location") == "ssh":
            add(project.get("ssh_target"), project.get("ssh_port") or None)
    try:
        with open(os.path.join(ssh_dir(), "config"), "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        words = line.strip().split()
        if len(words) > 1 and words[0].lower() == "host":
            for alias in words[1:]:
                if not any(c in alias for c in "*?!") and _target_ok(alias):
                    add(alias, None)
    return out


def _target_ok(target: str) -> bool:
    try:
        SSHHost(target)
    except HostError:
        return False
    return True


# ---- tunnels -------------------------------------------------------------------------

def _port(value, what: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"The {what} must be a whole number from 1 to 65535.") from None
    if isinstance(value, bool) or not 1 <= port <= 65535:
        raise ValueError(f"The {what} must be a whole number from 1 to 65535.")
    return port


def validate_tunnel(spec) -> dict:
    """The cleaned tunnel spec, or ValueError."""
    if not isinstance(spec, dict):
        raise ValueError("A tunnel must be an object.")
    tid = str(spec.get("id") or "").strip()
    if not TUNNEL_ID.match(tid):
        raise ValueError("A tunnel id is 1–32 lowercase letters, digits, - or _.")
    name = str(spec.get("name") or "").strip()
    if len(name) > 60 or any(c in name for c in "\r\n"):
        raise ValueError("The tunnel name must be a single line of up to 60 characters.")
    target = str(spec.get("target") or "").strip()
    port = spec.get("port")
    try:
        SSHHost(target, port if port not in (None, "") else None)
    except HostError as exc:
        raise ValueError(str(exc)) from None
    except (TypeError, ValueError):
        raise ValueError("The SSH port must be a whole number.") from None
    ssh_port = _port(port, "SSH port") if port not in (None, "") else None
    direction, bind = spec.get("direction"), spec.get("bind")
    if direction not in DIRECTIONS:
        raise ValueError("Direction must be local or remote.")
    if bind not in BINDS:
        raise ValueError("Bind address must be 127.0.0.1 or 0.0.0.0.")
    dest = str(spec.get("dest_host") or "").strip()
    if not dest or dest.startswith("-") or not DEST_HOST.match(dest):
        raise ValueError("The destination host may only contain letters, digits and . - : _ [ ].")
    return {"id": tid, "name": name or f"{target} {direction}", "target": target, "port": ssh_port,
            "direction": direction, "bind": bind, "listen_port": _port(spec.get("listen_port"), "listen port"),
            "dest_host": dest, "dest_port": _port(spec.get("dest_port"), "destination port")}


def clean_tunnels(values, strict: bool = False) -> list[dict]:
    """Validate saved tunnels. Invalid entries and repeated ids are dropped (an error if ``strict``)."""
    if not isinstance(values, list):
        if strict:
            raise ValueError("tunnels must be a list.")
        return []
    out, seen = [], set()
    for raw in values:
        try:
            if isinstance(raw, dict) and not str(raw.get("id") or "").strip():
                raw = {**raw, "id": _free_id(str(raw.get("name") or "tunnel"), seen, values)}
            spec = validate_tunnel(raw)
            if spec["id"] in seen:
                raise ValueError(f"Tunnel id {spec['id']!r} is used twice.")
        except ValueError:
            if strict:
                raise
            continue
        seen.add(spec["id"])
        out.append(spec)
    return out


def _free_id(name: str, seen: set, values: list) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:28].strip("-") or "tunnel"
    used = seen | {str(v.get("id")) for v in values if isinstance(v, dict)}
    tid, n = base, 2
    while tid in used:
        tid, n = f"{base}-{n}", n + 1
    return tid


def tunnel_argv(spec: dict) -> list[str]:
    flag = "-L" if spec["direction"] == "local" else "-R"
    forward = f"{spec['bind']}:{spec['listen_port']}:{spec['dest_host']}:{spec['dest_port']}"
    port_args = ["-p", str(spec["port"])] if spec.get("port") else []
    return ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30", *port_args, flag, forward, spec["target"]]


@dataclass
class Tunnel:
    spec: dict
    proc: Optional[subprocess.Popen] = None
    started: float = 0.0
    stderr: deque = field(default_factory=lambda: deque(maxlen=20))


class TunnelManager:
    """Runs and tracks ssh port-forward processes by tunnel id."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tunnels: dict[str, Tunnel] = {}

    def _alive(self, tid: str) -> bool:
        t = self._tunnels.get(tid)
        return bool(t and t.proc and t.proc.poll() is None)

    def start(self, spec: dict) -> None:
        spec = validate_tunnel(spec)
        with self._lock:
            if self._alive(spec["id"]):
                raise ValueError("That tunnel is already running.")
            try:
                proc = subprocess.Popen(
                    tunnel_argv(spec), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, **background_kwargs())
            except FileNotFoundError:
                raise HostError("Command not found: ssh") from None
            except OSError as exc:
                raise HostError(f"Could not start ssh: {exc}") from exc
            tunnel = Tunnel(spec, proc, time.time())
            self._tunnels[spec["id"]] = tunnel

        def pump():
            for raw in iter(proc.stderr.readline, b""):
                tunnel.stderr.append(raw.decode("utf-8", "replace").rstrip())
            proc.stderr.close()

        pumper = threading.Thread(target=pump, daemon=True)
        pumper.start()
        try:
            proc.wait(timeout=STARTUP_WAIT)
        except subprocess.TimeoutExpired:
            return
        pumper.join(timeout=2)
        tail = "\n".join(tunnel.stderr).strip()
        raise HostError(f"The tunnel stopped right away (exit {proc.returncode}): {tail or 'no output'}")

    def stop(self, tid: str) -> None:
        with self._lock:
            tunnel = self._tunnels.get(tid)
        if tunnel and tunnel.proc:
            kill_tree(tunnel.proc)

    def stop_all(self) -> None:
        with self._lock:
            tunnels = list(self._tunnels.values())
        for tunnel in tunnels:
            if tunnel.proc:
                kill_tree(tunnel.proc)

    def is_running(self, tid: str) -> bool:
        with self._lock:
            return self._alive(tid)

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for tid in self._tunnels if self._alive(tid))

    def status(self) -> dict:
        """``{id: {state, returncode, stderr, started}}`` for every tunnel started this session."""
        with self._lock:
            out = {}
            for tid, t in self._tunnels.items():
                code = t.proc.poll() if t.proc else None
                out[tid] = {"state": "running" if t.proc and code is None else "stopped",
                            "returncode": code, "stderr": "\n".join(t.stderr), "started": t.started}
            return out
