"""Plugins: self-contained developer tools that live beside the project board.

A plugin is a folder holding a Python package::

    myplugin/
      __init__.py          defines PLUGIN, a pluginkit.Plugin subclass
      templates/sidebar.html   optional: the plugin's sidebar pane
      templates/view.html      optional: the plugin's main view
      static/plugin.js         optional: loaded on the page; calls PG.register({...})
      static/plugin.css        optional: loaded on the page

Built-in plugins live in ``patchgoblin/plugins/``; drop extra ones into ``<data dir>/plugins/``
(or any folder listed in ``PATCHGOBLIN_PLUGINS``). Each one gets an activity-bar icon, API
routes under ``/api/<id>/``, its files under ``/plugins/<id>/`` and its own saved data. Plugins
are switched on and off in the Plugins dialog; a disabled plugin is stopped, its routes answer
404 and the page leaves it out.

Servers: the Server Manager (see servers.py) keeps one saved list of servers. A plugin adds a
server type by overriding ``Plugin.server_types()`` to return ``servers.ServerType`` objects (an
id, a label, ``Field`` specs and optional ``clean`` / ``test`` hooks); the Server Manager builds
its form from the field specs. Plugins read and write servers through ``ctx.servers``:
``list(type)``, ``get(id)`` (secrets included, for server-side use only), ``save(spec)`` and
``delete(id)``. Servers of a disabled plugin's type are kept and shown as unavailable.

Projects: a plugin that creates a folder can register it as a PatchGoblin project through
``ctx.add_project(data)``, which runs the same code as ``POST /api/projects``.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from typing import Callable

from flask import Blueprint, Flask, abort, request, send_from_directory
from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader

from . import servers as servermod

log = logging.getLogger(__name__)

PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# /api/<id> must not shadow the core API.
RESERVED_IDS = {"projects", "settings", "endpoints", "browse", "plugins", "static", "api", "servers", "server-types"}
BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")


class ServerHandle:
    """A plugin's view of the saved servers (``ctx.servers``). Writes are validated by the server
    type; reads include secrets, so never pass them to the browser (use ``servers.public_server``)."""

    def __init__(self, settings, types: dict):
        self.settings, self.types = settings, types

    def list(self, type: str | None = None) -> list[dict]:
        return [s for s in servermod.clean_servers(self.settings.servers()) if type is None or s.get("type") == type]

    def get(self, sid: str) -> dict | None:
        return next((s for s in self.list() if s["id"] == sid), None)

    def save(self, spec: dict) -> dict:
        """Create or update (by ``spec["id"]``) a server; returns the cleaned server. Raises ValueError."""
        with self.settings.file.lock:
            saved = self.settings.servers()
            server = servermod.clean_server(spec, saved, self.types)
            at = next((i for i, s in enumerate(saved) if s.get("id") == server["id"]), None)
            if at is None:
                saved.append(server)
            else:
                saved[at] = server
            self.settings.save_servers(saved)
        return server

    def delete(self, sid: str) -> bool:
        with self.settings.file.lock:
            saved = self.settings.servers()
            kept = [s for s in saved if s.get("id") != sid]
            if len(kept) != len(saved):
                self.settings.save_servers(kept)
            return len(kept) != len(saved)


@dataclass
class PluginContext:
    """What a plugin gets from PatchGoblin."""
    id: str
    app: Flask
    registry: object  # store.Registry: the projects
    settings: object  # store.Settings
    data_dir: str
    servers: ServerHandle | None = None  # the saved servers (see servers.py)
    # Registers a project: takes the fields of POST /api/projects, returns the project, and raises
    # ValueError / HostError. Set once the app has built it.
    add_project: Callable[[dict], dict] | None = None

    def load(self) -> dict:
        """This plugin's saved data (a dict, empty at first)."""
        return self.settings.plugin_data(self.id)

    def save(self, data: dict) -> dict:
        """Replace this plugin's saved data."""
        return self.settings.save_plugin_data(self.id, data)


class Plugin:
    """Base class for plugins. Subclass it and expose the subclass as ``PLUGIN``."""

    id = ""  # lowercase slug: the API prefix /api/<id>, the template prefix "<id>/" and the asset path
    name = ""  # shown on the activity bar and in the Plugins dialog
    description = ""
    icon = ""  # inner markup of a 24x24 stroke SVG (paths, circles…)
    order = 100  # activity-bar position, lowest first
    scripts: list[str] = []  # extra files under static/ loaded before plugin.js (e.g. vendored libraries)
    styles: list[str] = []  # extra stylesheets under static/ loaded before plugin.css

    def __init__(self, ctx: PluginContext):
        self.ctx = ctx

    def server_types(self) -> list:
        """Server types this plugin adds to the Server Manager (``servers.ServerType`` objects)."""
        return []

    def routes(self, bp: Blueprint) -> None:
        """Add API routes to ``bp`` (mounted at /api/<id>). Called once, enabled or not."""

    def start(self) -> None:
        """Called when the plugin is enabled: at startup, or when switched on."""

    def stop(self) -> None:
        """Called when the plugin is switched off and when PatchGoblin exits."""


@dataclass
class Loaded:
    """One discovered plugin folder: the instance, or why it could not be loaded."""
    id: str
    path: str
    builtin: bool
    plugin: Plugin | None = None
    error: str = ""
    running: bool = False
    templates: dict = field(default_factory=dict)  # "sidebar"/"view" -> template name
    assets: dict = field(default_factory=dict)  # "js"/"css" -> file name under static/

    @property
    def static_dir(self) -> str:
        return os.path.join(self.path, "static")


def plugin_dirs(data_dir: str) -> list[str]:
    """Where plugins are looked for, built-ins first."""
    extra = [d for d in os.environ.get("PATCHGOBLIN_PLUGINS", "").split(os.pathsep) if d.strip()]
    return [BUILTIN_DIR, os.path.join(data_dir, "plugins"), *extra]


def _import(folder: str, builtin: bool):
    name = os.path.basename(folder)
    if builtin:
        return importlib.import_module(f"patchgoblin.plugins.{name}")
    # A dropped-in plugin is imported under its own package name so its relative imports work.
    modname = f"patchgoblin_plugin_{re.sub(r'[^A-Za-z0-9_]', '_', name)}"
    spec = importlib.util.spec_from_file_location(modname, os.path.join(folder, "__init__.py"),
                                                  submodule_search_locations=[folder])
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(modname, None)
        raise
    return module


class PluginManager:
    def __init__(self, app: Flask, registry, settings, data_dir: str, dirs: list[str] | None = None):
        self.app, self.registry, self.settings, self.data_dir = app, registry, settings, data_dir
        self.dirs = plugin_dirs(data_dir) if dirs is None else dirs
        self.user_dir = os.path.join(data_dir, "plugins")
        self.items: list[Loaded] = []
        self.types: dict = servermod.core_types()  # server type id -> ServerType, plugins' included
        self._discover()
        self._wire()

    # ---- discovery ---------------------------------------------------------------
    def _discover(self) -> None:
        seen: set[str] = set()
        for base in self.dirs:
            if not os.path.isdir(base):
                continue
            builtin = os.path.normcase(os.path.abspath(base)) == os.path.normcase(BUILTIN_DIR)
            for name in sorted(os.listdir(base)):
                folder = os.path.join(base, name)
                if name.startswith((".", "_")) or not os.path.isfile(os.path.join(folder, "__init__.py")):
                    continue
                item = Loaded(id=name, path=folder, builtin=builtin)
                self.items.append(item)
                try:
                    module = _import(folder, builtin)
                    cls = getattr(module, "PLUGIN", None)
                    if not (isinstance(cls, type) and issubclass(cls, Plugin)):
                        raise ValueError("__init__.py must define PLUGIN, a pluginkit.Plugin subclass.")
                    pid = cls.id or name
                    if not PLUGIN_ID.match(pid) or pid in RESERVED_IDS:
                        raise ValueError(f"Invalid plugin id {pid!r}.")
                    item.id = pid
                    if pid in seen:
                        raise ValueError(f"Another plugin already uses the id {pid!r}.")
                    seen.add(pid)
                    item.plugin = cls(PluginContext(pid, self.app, self.registry, self.settings, self.data_dir,
                                                    ServerHandle(self.settings, self.types)))
                    added: list[str] = []
                    try:
                        for stype in item.plugin.server_types():
                            servermod.register_type(self.types, stype, pid)
                            added.append(stype.id)
                    except Exception:
                        for type_id in added:
                            servermod.unregister_type(self.types, type_id)
                        raise
                except Exception as exc:
                    item.error = f"{type(exc).__name__}: {exc}"
                    log.warning("Plugin %s failed to load:\n%s", folder, traceback.format_exc())
                    continue
                for part in ("sidebar", "view"):
                    if os.path.isfile(os.path.join(folder, "templates", f"{part}.html")):
                        item.templates[part] = f"{pid}/{part}.html"
                for kind in ("js", "css"):
                    if os.path.isfile(os.path.join(item.static_dir, f"plugin.{kind}")):
                        item.assets[kind] = f"plugin.{kind}"

    def _wire(self) -> None:
        app = self.app
        loaders = {}
        for item in self.ready():
            bp = Blueprint(f"plugin_{item.id}", f"patchgoblin_plugin_bp_{item.id}", url_prefix=f"/api/{item.id}")
            try:
                item.plugin.routes(bp)
                app.register_blueprint(bp)
            except Exception as exc:
                item.error = f"{type(exc).__name__}: {exc}"
                log.warning("Plugin %s routes failed:\n%s", item.id, traceback.format_exc())
                continue
            loaders[item.id] = FileSystemLoader(os.path.join(item.path, "templates"))
        # Plugin templates are named "<id>/<file>", so plugins can't shadow core templates or each other.
        app.jinja_env.loader = ChoiceLoader([app.jinja_env.loader, PrefixLoader(loaders)])

        @app.before_request
        def plugin_guard():
            bp = request.blueprint or ""
            if bp.startswith("plugin_") and not self.enabled(bp[len("plugin_"):]):
                abort(404)

        @app.get("/plugins/<pid>/<path:filename>")
        def plugin_asset(pid, filename):
            item = self.get(pid)
            if item is None or not self.enabled(pid):
                abort(404)
            return send_from_directory(item.static_dir, filename, max_age=0)

    def set_project_adder(self, fn) -> None:
        """Give every loaded plugin the core's project registration (``ctx.add_project``)."""
        for item in self.ready():
            item.plugin.ctx.add_project = fn

    # ---- lookups -----------------------------------------------------------------
    def ready(self) -> list[Loaded]:
        """Plugins that loaded (enabled or not)."""
        return [i for i in self.items if i.plugin is not None and not i.error]

    def get(self, pid: str) -> Loaded | None:
        return next((i for i in self.ready() if i.id == pid), None)

    def plugin(self, pid: str) -> Plugin | None:
        item = self.get(pid)
        return item.plugin if item else None

    def enabled(self, pid: str) -> bool:
        return self.get(pid) is not None and pid not in self.settings.disabled_plugins()

    def active(self) -> list[Loaded]:
        """Enabled plugins in activity-bar order."""
        return sorted((i for i in self.ready() if self.enabled(i.id)), key=lambda i: (i.plugin.order, i.id))

    # ---- lifecycle ---------------------------------------------------------------
    def _start(self, item: Loaded) -> None:
        if item.running:
            return
        try:
            item.plugin.start()
            item.running = True
        except Exception as exc:
            item.error = f"start failed: {exc}"
            log.warning("Plugin %s failed to start:\n%s", item.id, traceback.format_exc())

    def _stop(self, item: Loaded) -> None:
        if not item.running:
            return
        item.running = False
        try:
            item.plugin.stop()
        except Exception:
            log.warning("Plugin %s failed to stop:\n%s", item.id, traceback.format_exc())

    def start_enabled(self) -> None:
        for item in self.active():
            self._start(item)

    def stop_all(self) -> None:
        for item in self.items:
            if item.plugin is not None:
                self._stop(item)

    def set_enabled(self, pid: str, on: bool) -> None:
        item = self.get(pid)
        if item is None:
            raise KeyError(pid)
        self.settings.set_plugin_enabled(pid, on)
        (self._start if on else self._stop)(item)

    # ---- views -------------------------------------------------------------------
    def asset_url(self, item: Loaded, kind: str) -> str:
        return self.file_url(item, item.assets[kind])

    @staticmethod
    def file_url(item: Loaded, name: str) -> str | None:
        """URL of a file under the plugin's static/, versioned by its mtime (None if missing)."""
        path = os.path.join(item.static_dir, name)
        if not os.path.isfile(path):
            log.warning("Plugin %s: missing static file %s", item.id, name)
            return None
        return f"/plugins/{item.id}/{name}?v={int(os.path.getmtime(path))}"

    def page_view(self) -> list[dict]:
        """What index.html needs to show the enabled plugins."""
        def urls(names, own):
            out = [self.file_url(i, n) for n in names] + ([self.asset_url(i, own)] if own in i.assets else [])
            return [u for u in out if u]
        views = []
        for i in self.active():
            views.append({"id": i.id, "name": i.plugin.name or i.id, "icon": i.plugin.icon,
                          "sidebar": i.templates.get("sidebar"), "view": i.templates.get("view"),
                          "scripts": urls(i.plugin.scripts, "js"), "styles": urls(i.plugin.styles, "css")})
        return views

    def type_view(self) -> list[dict]:
        """Every server type for the Server Manager; ``available`` once its plugin is enabled."""
        return [t.view(not t.owner or self.enabled(t.owner)) for t in self.types.values()]

    def list_view(self) -> dict:
        """Every discovered plugin for the Plugins dialog, including broken ones."""
        rows = []
        for i in self.items:
            p = i.plugin
            rows.append({"id": i.id, "name": (p.name if p else "") or i.id, "description": p.description if p else "",
                         "path": i.path, "builtin": i.builtin, "error": i.error,
                         "enabled": not i.error and p is not None and self.enabled(i.id)})
        return {"plugins": rows, "user_dir": self.user_dir}
