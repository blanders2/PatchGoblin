"""Server Manager: one saved list of servers (SSH hosts, LLM APIs…) shared by the app and its plugins.

Each saved server is ``{id, name, type, …the type's fields}`` and lives in settings.json under
``servers``. A *server type* describes its fields (``Field``) and may add a ``clean`` hook (extra
validation / normalising) and a ``test`` hook (a connection check). The core registers ``ssh``;
plugins add types by overriding ``Plugin.server_types()``. The Server Manager dialog builds its
form from the field specs, so a plugin type needs no UI code of its own.

Secret fields (``secret=True``) are kept in settings.json but never sent to the browser: a blank
secret on save keeps the saved value unless ``clear_<key>`` is set (see ``public_server``).
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Callable

from .hosts import HostError, SSHHost

SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
KINDS = ("text", "number", "password", "select", "checkbox", "textarea")
SSH_OSES = ("auto", "posix", "windows")


@dataclass
class Field:
    """One input of a server type's form."""
    key: str
    label: str
    kind: str = "text"  # one of KINDS
    required: bool = False
    default: object = None
    options: list = field(default_factory=list)  # select: [(value, label)…]
    placeholder: str = ""
    help: str = ""
    secret: bool = False  # kept server-side only; a blank value keeps the saved one

    def view(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind, "required": self.required,
                "default": self.default, "options": [{"value": v, "label": l} for v, l in self.options],
                "placeholder": self.placeholder, "help": self.help, "secret": self.secret}


@dataclass
class ServerType:
    id: str
    label: str
    fields: list[Field]
    clean: Callable[[dict, dict | None], dict] | None = None  # (values, old server) -> the cleaned fields
    test: Callable[[dict], dict] | None = None  # (full server incl. secrets) -> {"message": …}; raises on failure
    owner: str = ""  # the plugin id that registered the type ("" = the core)

    def view(self, available: bool = True) -> dict:
        return {"id": self.id, "label": self.label, "owner": self.owner, "available": available,
                "testable": self.test is not None, "fields": [f.view() for f in self.fields]}


# ---- the type registry -----------------------------------------------------------------
# A registry is a plain dict {type id: ServerType}. Each PluginManager owns one, seeded with the
# core's types (see core_types), so separate app instances never share or clash.

def core_types() -> dict[str, ServerType]:
    return {SSH_TYPE.id: SSH_TYPE}


def register_type(types: dict, stype: ServerType, owner: str = "") -> ServerType:
    if not SERVER_ID.match(stype.id):
        raise ValueError(f"Invalid server type id {stype.id!r}.")
    if stype.id in types:
        raise ValueError(f"The server type {stype.id!r} is already registered"
                         f"{' by the ' + types[stype.id].owner + ' plugin' if types[stype.id].owner else ''}.")
    for f in stype.fields:
        if f.kind not in KINDS:
            raise ValueError(f"Server type {stype.id!r}: unknown field kind {f.kind!r}.")
    stype.owner = owner
    types[stype.id] = stype
    return stype


def unregister_type(types: dict, type_id: str) -> None:
    types.pop(type_id, None)


# ---- validation ------------------------------------------------------------------------

def free_id(name: str, taken) -> str:
    """A server id made from ``name`` that isn't in ``taken``."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:28] or "server"
    sid, n = slug, 2
    while sid in taken:
        sid, n = f"{slug[:28]}-{n}", n + 1
    return sid


_MISSING = object()  # a spec without a key keeps the saved value; an explicit null/blank clears it


def _coerce(f: Field, value, old_has: bool, old_value):
    """One field's cleaned value; ``value`` is _MISSING when the spec didn't include the key."""
    if f.kind == "checkbox":
        if value is _MISSING:
            return old_value if old_has else bool(f.default)
        return bool(value)
    if value is _MISSING:
        if old_has:
            return old_value
        value = f.default
    if f.kind == "number":
        if value in (None, ""):
            number = None
        else:
            try:
                number = float(value) if isinstance(value, float) else int(str(value).strip())
            except (TypeError, ValueError):
                raise ValueError(f"{f.label} must be a number.") from None
            if isinstance(value, bool):
                raise ValueError(f"{f.label} must be a number.")
        if number is None and f.required:
            raise ValueError(f"{f.label} is required.")
        return number
    if f.kind == "textarea" and isinstance(value, dict):
        return value  # a structured value kept as it is (the type's clean hook checks it)
    text = "" if value is None else str(value)
    text = text.strip() if f.kind != "textarea" else text.strip("\r\n")
    if f.required and not text:
        raise ValueError(f"{f.label} is required.")
    if f.kind == "select" and text and text not in [v for v, _ in f.options]:
        raise ValueError(f"{f.label}: {text!r} is not one of the choices.")
    if f.kind == "select" and not text:
        text = "" if f.default is None else str(f.default)
    return text


def clean_server(spec, saved: list[dict], types: dict[str, ServerType]) -> dict:
    """Validate a server from a form or a plugin. Updates the saved server whose id matches;
    a blank secret keeps the saved one unless ``clear_<key>`` is set."""
    if not isinstance(spec, dict):
        raise ValueError("server must be an object.")
    stype = types.get(str(spec.get("type") or ""))
    if stype is None:
        raise ValueError(f"Unknown server type {spec.get('type')!r}.")
    sid = str(spec.get("id") or "").strip()
    old = next((s for s in saved if s.get("id") == sid), None) if sid else None
    if sid and old is None and not SERVER_ID.match(sid):
        raise ValueError("A server id is 1–32 lowercase letters, digits, - or _.")
    if old is not None and old.get("type") != stype.id:
        raise ValueError(f"The server {sid!r} is not a {stype.label} server.")
    name = str(spec.get("name") or "").strip()
    if len(name) > 60 or any(c in name for c in "\r\n"):
        raise ValueError("The server name must be a single line of up to 60 characters.")
    values = {}
    for f in stype.fields:
        old_has = old is not None and f.key in old
        raw = spec.get(f.key, _MISSING)
        if f.secret:
            text = "" if raw is None or raw is _MISSING else str(raw).strip()
            if not text and old_has and not spec.get(f"clear_{f.key}"):
                text = str(old[f.key])
            values[f.key] = text
        else:
            values[f.key] = _coerce(f, raw, old_has, old[f.key] if old_has else None)
    if stype.clean:
        extra = stype.clean(dict(values), old)
        values = {**values, **(extra or {})}
        name = name or str(values.pop("name", "") or "").strip()
    values.pop("name", None)
    if not name:
        name = str(values.get("target") or values.get("base_url") or "").strip()
    if not sid:
        sid = free_id(name, {s.get("id") for s in saved})
    elif old is None and any(s.get("id") == sid for s in saved):
        raise ValueError(f"Server id {sid!r} is used twice.")
    return {"id": sid, "name": name or sid, "type": stype.id, **values}


def clean_servers(values) -> list[dict]:
    """Saved servers, lenient: malformed entries and repeated ids are dropped. A server whose
    type isn't registered (its plugin is off or gone) is kept as it is."""
    out: list[dict] = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict) or not SERVER_ID.match(str(value.get("id") or "")):
            continue
        if any(s["id"] == value["id"] for s in out):
            continue
        out.append(dict(value))
    return out


_SECRET_LOOKING = re.compile(r"key|secret|token|passw|credential", re.IGNORECASE)


def public_server(server: dict, stype: ServerType | None) -> dict:
    """A server for the browser: secret fields are removed, replaced by ``<key>_saved`` flags.
    For a server of an unknown type (its plugin is gone) every secret-looking field is removed."""
    if stype is not None:
        secrets = {f.key for f in stype.fields if f.secret}
    else:
        secrets = {k for k in server if _SECRET_LOOKING.search(k) and not k.endswith("_env")}
    out = {k: v for k, v in server.items() if k not in secrets}
    for key in secrets:
        out[f"{key}_saved"] = bool(server.get(key))
    return out


# ---- the built-in SSH type -------------------------------------------------------------

def normalize_mac(value) -> str:
    """A MAC address in lowercase colon form, "" when empty, or ValueError."""
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"[:.\-]", "", text).lower()
    if not re.fullmatch(r"[0-9a-f]{12}", digits):
        raise ValueError("A MAC address is 12 hex digits, e.g. aa:bb:cc:dd:ee:ff.")
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def check_ipv4(value: str) -> str:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        raise ValueError("The wake-up address must be an IPv4 address such as 192.168.1.255.") from None
    return value


def ssh_port(value, what: str = "SSH port") -> int | None:
    if value in (None, ""):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"The {what} must be a whole number from 1 to 65535.") from None
    if isinstance(value, bool) or not 1 <= port <= 65535:
        raise ValueError(f"The {what} must be a whole number from 1 to 65535.")
    return port


def _clean_ssh(values: dict, old: dict | None) -> dict:
    target = values["target"]
    try:
        SSHHost(target, None)
    except HostError as exc:
        raise ValueError(str(exc)) from None
    wol = values.get("wol_address") or ""
    return {"port": ssh_port(values.get("port")), "os": values.get("os") or "auto",
            "mac": normalize_mac(values.get("mac")), "wol_address": check_ipv4(wol) if wol else ""}


def _test_ssh(server: dict) -> dict:
    host = SSHHost(server["target"], server.get("port"))
    host.check()
    return {"message": f"✓ Connected to {host.label}"}


SSH_TYPE = ServerType(
    id="ssh", label="SSH host",
    fields=[
        Field("target", "SSH host", required=True, placeholder="user@host or ssh-config alias"),
        Field("port", "SSH port", kind="number", placeholder="22"),
        Field("os", "OS", kind="select", default="auto",
              options=[("auto", "Auto-detect"), ("posix", "Linux/macOS"), ("windows", "Windows")]),
        Field("mac", "MAC address (wake-on-LAN)", placeholder="aa:bb:cc:dd:ee:ff"),
        Field("wol_address", "Wake-up address", placeholder="255.255.255.255",
              help="The wake-up packet is sent from this computer, so the server must be on the same LAN, or this "
                   "must be its subnet's broadcast address (e.g. 192.168.1.255)."),
    ],
    clean=_clean_ssh, test=_test_ssh)
