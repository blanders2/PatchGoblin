"""SVN bookkeeping for projects that are an existing Subversion working copy.

A Subversion commit is published to the server at once, so PatchGoblin never commits after
a task. It records the files each run changed on the task and commits them all together when
the user clicks Check in."""
from __future__ import annotations

import ntpath
import re
import xml.etree.ElementTree as ET

from .hosts import HostError

ITEMS = ("modified", "added", "deleted", "unversioned", "missing", "replaced", "conflicted")
LETTER = {"modified": "M", "added": "A", "deleted": "D", "unversioned": "?", "missing": "D",
          "replaced": "M", "conflicted": "M"}
_REVISION = re.compile(r"Committed revision (\d+)")


def svn(host, path: str, *args: str, timeout: float = 300):
    return host.run(["svn", "--non-interactive", *args], cwd=path, timeout=timeout)


def _check(res, what: str):
    if not res.ok:
        raise HostError(f"svn {what} failed: {(res.stderr or res.stdout).strip()} "
                        "(PatchGoblin runs svn non-interactively, so credentials must already be cached)")
    return res


def tracked(project: dict) -> bool:
    """Whether SVN check-in tracking is turned on for this project."""
    return project.get("svn_tracking") is True


def _same_path(host, a: str, b: str) -> bool:
    a, b = host.normalize(a.strip()), host.normalize(b.strip())
    if "\\" in a or "\\" in b or (len(a) > 1 and a[1] == ":"):  # a Windows path: ignore case and slashes
        return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))
    return a.rstrip("/") == b.rstrip("/")


def is_wc_root(host, path: str) -> bool:
    res = svn(host, path, "info", "--show-item", "wc-root", ".")
    if not res.ok or not res.stdout.strip():
        return False
    try:
        return _same_path(host, res.stdout, path)
    except HostError:
        return False


def status_entries(host, path: str) -> dict[str, str]:
    """``{relpath: item}`` from ``svn status --xml``, leaving out ignored files and
    ``.patchgoblin/``."""
    res = _check(svn(host, path, "status", "--xml"), "status")
    try:
        root = ET.fromstring(res.stdout.encode("utf-8") if isinstance(res.stdout, str) else res.stdout)
    except ET.ParseError as exc:
        raise HostError(f"svn status gave unreadable output: {exc}") from exc
    entries: dict[str, str] = {}
    for entry in root.iter("entry"):
        name = (entry.get("path") or "").replace("\\", "/")
        wc = entry.find("wc-status")
        item = wc.get("item") if wc is not None else ""
        if item not in ITEMS:
            if wc is not None and wc.get("props") in ("modified", "conflicted"):
                item = "modified"  # property-only change
            else:
                continue
        if name == ".patchgoblin" or name.startswith(".patchgoblin/"):
            continue
        entries[name] = item
    return entries


def vcs_summary(host, path: str) -> dict:
    """A working copy always has a repository; "pushed" means nothing is waiting to be checked in."""
    return {"kind": "svn", "remote": True, "pending": len(status_entries(host, path))}


def dirty_fingerprint(host, path: str) -> dict[str, str]:
    """``{path: "item blob"}`` for every changed file. Comparing two fingerprints also catches
    edits to files that were already modified. Blobs come from ``git hash-object``."""
    status = status_entries(host, path)
    present = [name for name, item in status.items()
               if item not in ("deleted", "missing") and not host.is_dir(host.join(path, name))]
    blobs: dict[str, str] = {}
    if present:
        res = host.run(["git", "hash-object", "--no-filters", "--stdin-paths"], cwd=path,
                       input="\n".join(present) + "\n", timeout=300)
        hashes = res.stdout.split() if res.ok else []
        if len(hashes) == len(present):  # otherwise (e.g. a directory) compare status only
            blobs = dict(zip(present, hashes))
    return {name: f"{item} {blobs.get(name, '')}" for name, item in status.items()}


def changed_between(before: dict[str, str], after: dict[str, str]) -> list[dict]:
    """Files whose fingerprint differs, as ``{"status", "path"}`` (like ``gitops.commit_files``)."""
    files = []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name):
            continue
        item = (after.get(name) or before.get(name)).split(" ", 1)[0]
        files.append({"status": LETTER.get(item, "M"), "path": name})
    return files


def checkin(host, path: str, message: str) -> str:
    """Add new files, remove missing ones and commit. Returns ``rN``, or "" if nothing changed."""
    if not status_entries(host, path):
        return ""
    _check(svn(host, path, "add", "--force", "--parents", "--depth", "infinity", "."), "add")
    for name, item in status_entries(host, path).items():
        if item == "missing":
            _check(svn(host, path, "delete", "--keep-local", "--", name + "@" if "@" in name else name), "delete")
    res = _check(svn(host, path, "commit", "-m", message), "commit")
    found = _REVISION.search(res.stdout)
    if not found:
        return ""  # svn committed nothing (e.g. only unversioned-and-ignored changes)
    return f"r{found.group(1)}"


def recent_log(host, path: str, limit: int = 30) -> list[dict]:
    res = svn(host, path, "log", "--xml", "-l", str(limit))
    if not res.ok:
        return []
    try:
        root = ET.fromstring(res.stdout)
    except ET.ParseError:
        return []
    commits = []
    for entry in root.iter("logentry"):
        msg = (entry.findtext("msg") or "").strip()
        commits.append({"hash": f"r{entry.get('revision')}", "author": entry.findtext("author") or "",
                        "when": (entry.findtext("date") or "")[:16].replace("T", " "),
                        "subject": msg.splitlines()[0] if msg else ""})
    return commits


def default_checkin_message(tasks: list[dict]) -> str:
    """A commit message listing the finished tasks, each with a trimmed summary."""
    if not tasks:
        return "PatchGoblin: manual check-in"
    if len(tasks) == 1:
        lines = [f"PatchGoblin: task #{tasks[0]['id']} {tasks[0]['title']}"]
    else:
        ids = ", ".join(f"#{t['id']}" for t in tasks)
        lines = [f"PatchGoblin: {len(tasks)} tasks ({ids})"]
    for t in tasks:
        lines += ["", f"- #{t['id']} {t['title']}"]
        summary = (t.get("summary") or "").strip()
        if len(summary) > 400:
            summary = summary[:400].rstrip() + "…"
        lines += [f"  {ln}" if ln.strip() else "" for ln in summary.splitlines()]
    return "\n".join(lines) + "\n"
