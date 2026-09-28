"""Git bookkeeping for projects: every project is its own repository, and each
completed task is recorded as a commit. Nothing is ever pushed."""
from __future__ import annotations

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
"""

FALLBACK_IDENTITY = ["-c", "user.name=PatchGoblin", "-c", "user.email=patchgoblin@localhost"]


def git(host, path: str, *args: str, timeout: float = 300):
    return host.run(["git", *args], cwd=path, timeout=timeout)


def _check(res, what: str):
    if not res.ok:
        raise HostError(f"git {what} failed: {(res.stderr or res.stdout).strip()}")
    return res


def is_repo_root(host, path: str) -> bool:
    res = git(host, path, "rev-parse", "--show-cdup")
    return res.ok and res.stdout.strip() == ""


def _identity(host, path: str) -> list[str]:
    """Use the user's own git identity when configured, otherwise a local fallback."""
    return [] if git(host, path, "config", "user.email").ok else FALLBACK_IDENTITY


def has_changes(host, path: str, ignore_metadata: bool = False) -> bool:
    res = _check(git(host, path, "status", "--porcelain", "--untracked-files=all"), "status")
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if ignore_metadata:
        lines = [ln for ln in lines if not ln[3:].strip('"').startswith(".patchgoblin/")]
    return bool(lines)


def commit_all(host, path: str, message: str) -> str:
    """Stage everything and commit. Returns the new commit hash, or "" if nothing changed."""
    if not has_changes(host, path):
        return ""
    _check(git(host, path, "add", "-A"), "add")
    _check(git(host, path, *_identity(host, path), "commit", "-q", "-m", message), "commit")
    return _check(git(host, path, "rev-parse", "HEAD"), "rev-parse").stdout.strip()


def ensure_repo(host, path: str) -> bool:
    """Make ``path`` the root of its own repository. Returns True if one was created.

    A directory nested inside some other repository still gets its own repo, so
    PatchGoblin's ``git add -A`` can never sweep up files outside the project.
    """
    if is_repo_root(host, path):
        return False
    _check(git(host, path, "init", "-q"), "init")
    gitignore = host.join(path, ".gitignore")
    if host.read_text(gitignore) is None:
        host.write_text(gitignore, DEFAULT_GITIGNORE)
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
