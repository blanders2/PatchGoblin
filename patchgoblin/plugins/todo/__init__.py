"""Todo plugin: one global checklist, optionally tagged with a project, kept in the plugin's saved data."""
from __future__ import annotations

import threading
import time

from flask import abort, jsonify, request

from patchgoblin.pluginkit import Plugin

from . import todos


def body() -> dict:
    return request.get_json(silent=True) or {}


class TodoPlugin(Plugin):
    id = "todo"
    name = "Todo"
    description = "A personal checklist with optional project tags and a badge counting open items."
    icon = '<rect x="4" y="4" width="16" height="16" rx="2"/><polyline points="8 12 11 15 16 9"/>'
    order = 40

    def __init__(self, ctx):
        super().__init__(ctx)
        self.lock = threading.Lock()  # every load-change-save runs under it

    def items(self) -> list[dict]:
        return todos.clean_items(self.ctx.load().get("items"))

    def _mutate(self, fn) -> list[dict]:
        """Load the items, let ``fn`` change them (returning the new list), save and return them."""
        with self.lock:
            data = self.ctx.load()
            items = fn(todos.clean_items(data.get("items")))
            self.ctx.save({**data, "items": items})
            return items

    def routes(self, bp) -> None:
        def view(items=None) -> dict:
            items = self.items() if items is None else items
            return {"items": items, "open": sum(1 for i in items if not i["done"])}

        def find(items, iid) -> dict:
            for item in items:
                if item["id"] == iid:
                    return item
            abort(404)

        @bp.get("/items")
        def get_items():
            return jsonify(view())

        @bp.post("/items")
        def add_item():
            data = body()
            item = todos.validate_item({"text": data.get("text"), "project": data.get("project")})

            def add(items):
                if len(items) >= todos.MAX_ITEMS:
                    raise ValueError(f"The list is full ({todos.MAX_ITEMS} items). Clear some done items first.")
                return [item, *items]

            return jsonify(view(self._mutate(add))), 201

        @bp.patch("/items/<iid>")
        def edit_item(iid):
            data = body()

            def edit(items):
                old = find(items, iid)
                new = {**old}
                if "text" in data:
                    new["text"] = data["text"]
                if "project" in data:
                    new["project"] = data["project"]
                if "done" in data:
                    if not isinstance(data["done"], bool):
                        raise ValueError("done must be true or false.")
                    if data["done"] != old["done"]:
                        new["done"] = data["done"]
                        new["done_at"] = time.time() if data["done"] else None
                checked = todos.validate_item(new)
                return [checked if i["id"] == iid else i for i in items]

            return jsonify(view(self._mutate(edit)))

        @bp.delete("/items/<iid>")
        def delete_item(iid):
            def delete(items):
                find(items, iid)
                return [i for i in items if i["id"] != iid]

            return jsonify(view(self._mutate(delete)))

        @bp.put("/order")
        def put_order():
            ids = body().get("ids")
            return jsonify(view(self._mutate(lambda items: todos.reorder(items, ids))))

        @bp.post("/clear-done")
        def clear_done():
            return jsonify(view(self._mutate(lambda items: [i for i in items if not i["done"]])))


PLUGIN = TodoPlugin
