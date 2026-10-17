"""Pure helpers for the Todo plugin: validating, cleaning and reordering checklist items."""
from __future__ import annotations

import time
import uuid

MAX_ITEMS = 500
MAX_TEXT = 500


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def clean_text(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Item text must be text.")
    text = value.strip()
    if not text:
        raise ValueError("Enter some text for the item.")
    if "\n" in text or "\r" in text:
        raise ValueError("Item text must be a single line.")
    if len(text) > MAX_TEXT:
        raise ValueError(f"Item text can be at most {MAX_TEXT} characters.")
    return text


def validate_item(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Each item must be an object.")
    done = raw.get("done", False)
    if not isinstance(done, bool):
        raise ValueError("done must be true or false.")
    project = raw.get("project") or ""
    if not isinstance(project, str):
        raise ValueError("project must be a project id.")
    created = raw.get("created")
    done_at = raw.get("done_at")
    return {
        "id": str(raw.get("id") or "").strip() or new_id(),
        "text": clean_text(raw.get("text")),
        "done": done,
        "project": project.strip(),
        "created": created if isinstance(created, (int, float)) and not isinstance(created, bool) else time.time(),
        "done_at": done_at if done and isinstance(done_at, (int, float)) and not isinstance(done_at, bool) else None,
    }


def clean_items(values, strict: bool = False) -> list[dict]:
    """Validate saved items. Invalid entries and repeated ids are dropped (an error if ``strict``)."""
    if not isinstance(values, list):
        if strict:
            raise ValueError("items must be a list.")
        return []
    out, seen = [], set()
    for raw in values:
        try:
            item = validate_item(raw)
            if item["id"] in seen:
                raise ValueError(f"Item id {item['id']!r} is used twice.")
        except ValueError:
            if strict:
                raise
            continue
        seen.add(item["id"])
        out.append(item)
    return out[:MAX_ITEMS]


def reorder(items: list[dict], ids) -> list[dict]:
    """The items in the order of ``ids``, which must be exactly the current set of ids."""
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("ids must be a list of item ids.")
    by_id = {item["id"]: item for item in items}
    if len(ids) != len(set(ids)) or set(ids) != set(by_id):
        raise ValueError("The list changed elsewhere. Reload it and try again.")
    return [by_id[i] for i in ids]
