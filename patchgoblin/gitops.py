"""Git bookkeeping for projects: every project is its own repository, and each
completed task is recorded as a commit. Commits are only pushed by an explicit
sync with the ``origin`` remote: a manual Sync, or the project's opt-in auto-sync."""
from __future__ import annotations

import contextlib
import re

from .hosts import HostError

DEFAULT_GITIGNORE = """\
# Added by PatchGoblin when it created this repository.
node_modules/
.venv/
venv/
__pycache__/
*.pyc
.env
.env.*
.DS_Store

# PatchGoblin's task metadata. Remove this line to commit and sync tasks.
.patchgoblin/
"""

FALLBACK_IDENTITY = ["-c", "user.name=PatchGoblin", "-c", "user.email=patchgoblin@localhost"]


REMOTE = "origin"
SYNC_MODES = ("ff-only", "rebase")
# Talking to a remote must fail fast instead of waiting on a credential prompt nobody can see.
NOPROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "SSH_ASKPASS_REQUIRE": "never"}
NOPROMPT_ARGS = ["-c", "credential.interactive=never"]


def git(host, path: str, *args: str, timeout: float = 300, env: dict | None = None):
    return host.run(["git", *args], cwd=path, timeout=timeout, env=env)


def _remote_git(host, path: str, *args: str):
    return git(host, path, *NOPROMPT_ARGS, *args, env=NOPROMPT_ENV)


def _check(res, what: str):
    if not res.ok:
        raise HostError(f"git {what} failed: {(res.stderr or res.stdout).strip()}")
    return res


def tracked(project: dict) -> bool:
    """Whether git tracking is turned on for this project."""
    return project.get("git_tracking") is True


def is_repo_root(host, path: str) -> bool:
    res = git(host, path, "rev-parse", "--show-cdup")
    return res.ok and res.stdout.strip() == ""


def _identity(host, path: str) -> list[str]:
    """Use the user's own git identity when configured, otherwise a local fallback."""
    return [] if git(host, path, "config", "user.email").ok else FALLBACK_IDENTITY


def status_lines(host, path: str, ignore_metadata: bool = False) -> list[str]:
    """``git status --porcelain`` lines (untracked files listed one by one)."""
    res = _check(git(host, path, "status", "--porcelain", "--untracked-files=all"), "status")
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if ignore_metadata:
        lines = [ln for ln in lines if not ln[3:].strip('"').startswith(".patchgoblin/")]
    return lines


def dirty_fingerprint(host, path: str) -> dict[str, str]:
    """``{path: "XY blob"}`` for every changed or untracked file outside ``.patchgoblin/``.

    Comparing two fingerprints also catches edits to files that were already dirty, which
    status lines alone miss. Deleted files have an empty blob."""
    res = _check(git(host, path, "status", "--porcelain", "-z", "--untracked-files=all"), "status")
    entries = res.stdout.split("\0")
    status: dict[str, str] = {}
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, name = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # -z lists a rename as "XY new\0old"; skip the old path
        if not name.startswith(".patchgoblin/"):
            status[name] = xy
    present = [name for name, xy in status.items() if "D" not in xy]
    blobs: dict[str, str] = {}
    if present:
        res = host.run(["git", "hash-object", "--stdin-paths"], cwd=path, input="\n".join(present) + "\n",
                       timeout=300)
        hashes = res.stdout.split() if res.ok else []
        if len(hashes) == len(present):  # otherwise (e.g. a submodule path) compare status only
            blobs = dict(zip(present, hashes))
    return {name: f"{xy} {blobs.get(name, '')}" for name, xy in status.items()}


def has_changes(host, path: str, ignore_metadata: bool = False) -> bool:
    return bool(status_lines(host, path, ignore_metadata))


def commit_all(host, path: str, message: str) -> str:
    """Stage everything and commit. Returns the new commit hash, or "" if nothing changed."""
    if not has_changes(host, path):
        return ""
    _check(git(host, path, "add", "-A"), "add")
    _check(git(host, path, *_identity(host, path), "commit", "-q", "-m", message), "commit")
    return _check(git(host, path, "rev-parse", "HEAD"), "rev-parse").stdout.strip()


def ensure_repo(host, path: str, template: str = DEFAULT_GITIGNORE) -> bool:
    """Make ``path`` the root of its own repository. Returns True if one was created.
    A new repository gets ``template`` as its .gitignore unless the folder already has one.

    A directory nested inside some other repository still gets its own repo, so
    PatchGoblin's ``git add -A`` can never sweep up files outside the project.
    """
    if is_repo_root(host, path):
        return False
    _check(git(host, path, "init", "-q"), "init")
    gitignore = host.join(path, ".gitignore")
    if host.read_text(gitignore) is None:
        host.write_text(gitignore, template)
    return True


def recent_commits(host, path: str, limit: int = 30) -> list[dict]:
    res = git(host, path, "log", f"-{limit}", "--pretty=format:%h%x1f%an%x1f%ar%x1f%s")
    if not res.ok:
        return []  # a fresh repository with no commits yet
    commits = []
    for line in res.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append(dict(zip(("hash", "author", "when", "subject"), parts)))
    return commits


_SHA = re.compile(r"^[0-9a-fA-F]{4,64}$")


def commit_files(host, path: str, sha: str) -> list[dict]:
    """The files a commit changed, as ``{"status", "path"}``, leaving out PatchGoblin's own
    metadata. [] for an unknown or malformed sha."""
    if not _SHA.match(sha or ""):
        return []
    res = git(host, path, "-c", "core.quotePath=false", "show", "--name-status", "--format=", sha, "--")
    if not res.ok:
        return []
    files = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[-1]  # a rename lists old and new paths; show the new one
        if not name.startswith(".patchgoblin/"):
            files.append({"status": parts[0][:1], "path": name})
    return files


def list_files(host, path: str, is_tracked: bool) -> list[str]:
    """Every text file under ``path`` that respects .gitignore, for the OpenAI agent's
    ``list_files`` tool.

    Tracked projects use the repository's own index. Untracked projects (possibly nested
    inside some other repository) must never resolve a parent repo, so ``--no-index`` is
    used instead; this also means empty and binary files are left out.
    """
    if is_tracked:
        res = git(host, path, "ls-files", "--cached", "--others", "--exclude-standard")
        return [n for n in res.stdout.splitlines() if n]
    res = git(host, path, "grep", "--no-index", "--exclude-standard", "-l", "-I", "-e", "")
    names = [n for n in res.stdout.splitlines() if n]
    return [n for n in names if not n.startswith((".git/", ".svn/", ".patchgoblin/"))]


# ---- remote sync ------------------------------------------------------------
_BAD_URL = re.compile(r"[\s\x00-\x1f\x7f]")


def get_remote(host, path: str, name: str = REMOTE) -> str:
    res = git(host, path, "remote", "get-url", name)
    return res.stdout.strip() if res.ok else ""


def set_remote(host, path: str, url: str, name: str = REMOTE) -> None:
    """Point ``name`` at ``url``; an empty url removes the remote."""
    url = (url or "").strip()
    if not url:
        git(host, path, "remote", "remove", name)  # a missing remote is fine
        return
    if url.startswith("-") or _BAD_URL.search(url):
        raise ValueError("Remote URL must not start with '-' or contain spaces or control characters.")
    verb = "set-url" if get_remote(host, path, name) else "add"
    _check(git(host, path, "remote", verb, name, url), f"remote {verb}")


def current_branch(host, path: str) -> str:
    res = git(host, path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not res.ok or not res.stdout.strip():
        raise HostError("detached HEAD; cannot sync")
    return res.stdout.strip()


def _has_head(host, path: str) -> bool:
    return git(host, path, "rev-parse", "--verify", "--quiet", "HEAD").ok


def remote_status(host, path: str) -> dict:
    """Remote, branch and ahead/behind counts from local refs only (never fetches)."""
    url = get_remote(host, path)
    res = git(host, path, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = res.stdout.strip() if res.ok else ""
    res = git(host, path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    upstream = res.stdout.strip() if res.ok else ""
    ahead = behind = 0
    if upstream:
        res = git(host, path, "rev-list", "--left-right", "--count", "HEAD...@{u}")
        parts = res.stdout.split() if res.ok else []
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
    elif url and _has_head(host, path):
        res = git(host, path, "rev-list", "--count", "HEAD")
        ahead = int(res.stdout.strip() or 0) if res.ok else 0
    return {"url": url, "branch": branch, "upstream": upstream, "ahead": ahead, "behind": behind,
            "dirty": has_changes(host, path)}


def sync(host, path: str, mode: str = "ff-only", push: bool = True, checkpoint: bool = True,
         message: str = "PatchGoblin: checkpoint before sync", guard=None) -> dict:
    """Fetch origin, bring in its commits for the current branch, then push.

    Never creates merge commits: ``ff-only`` refuses diverged history and ``rebase``
    replays local commits on top of origin's (aborting cleanly on conflict).
    ``guard`` is a context manager held while the working tree is being changed, so
    nothing else writes to it (e.g. tasks.json) mid-merge.
    """
    if mode not in SYNC_MODES:
        raise ValueError(f"Unknown sync mode {mode!r}.")
    if not get_remote(host, path):
        raise HostError(f"no remote configured; set the {REMOTE} URL first")
    guard = guard if guard is not None else contextlib.nullcontext()
    branch = current_branch(host, path)
    log = []

    _check(_remote_git(host, path, "fetch", REMOTE), "fetch")
    log.append(f"Fetched {REMOTE}")
    tracking = f"refs/remotes/{REMOTE}/{branch}"
    with guard:
        if checkpoint and has_changes(host, path):
            sha = commit_all(host, path, message)
            log.append(f"Committed local changes as {sha[:10]}")
        if not git(host, path, "rev-parse", "--verify", "--quiet", tracking).ok:
            log.append(f"{REMOTE} has no branch {branch} yet")
        elif mode == "rebase" and _has_head(host, path):
            res = git(host, path, *_identity(host, path), "rebase", tracking)
            if not res.ok:
                git(host, path, "rebase", "--abort")
                raise HostError(f"git rebase onto {REMOTE}/{branch} failed and was aborted: "
                                f"{(res.stderr or res.stdout).strip()}")
            log.append(f"Rebased onto {REMOTE}/{branch}")
        else:
            res = git(host, path, "merge", "--ff-only", tracking)
            if not res.ok:
                raise HostError(f"Could not fast-forward to {REMOTE}/{branch} (histories have diverged? "
                                f"try rebase mode): {(res.stderr or res.stdout).strip()}")
            log.append(f"Fast-forwarded to {REMOTE}/{branch}")

    if push:
        if not _has_head(host, path):
            raise HostError("nothing to push: the repository has no commits yet")
        _check(_remote_git(host, path, "push", "-u", REMOTE, branch), "push")
        log.append(f"Pushed {branch} to {REMOTE}")
    return {"log": log, **remote_status(host, path)}
