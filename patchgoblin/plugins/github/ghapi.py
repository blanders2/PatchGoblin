"""GitHub REST API access for the GitHub plugin. The token stays on this side of the browser."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from patchgoblin.hosts import HostError

DEFAULT_API = "https://api.github.com"
API_VERSION = "2022-11-28"
MAX_PAGES = 20
TIMEOUT = 30
PROTOCOLS = ("ssh", "https")
_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def api_base(server: dict) -> str:
    return (server.get("api_url") or DEFAULT_API).strip().rstrip("/")


def token_of(server: dict) -> str:
    token = (server.get("token") or "").strip()
    env = (server.get("token_env") or "").strip()
    return token or (os.environ.get(env, "").strip() if env else "")


def scrub(text: str, server: dict) -> str:
    token = token_of(server)
    return text.replace(token, "…") if token else text


def _open(server: dict, url: str):
    token = token_of(server)
    if not token:
        raise ValueError("This GitHub account has no token: add one in the Server Manager.")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION, "User-Agent": "PatchGoblin"})
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ValueError("GitHub rejected the token (401): it is wrong, expired or revoked.") from None
        limited = exc.headers.get("X-RateLimit-Remaining") == "0" or exc.headers.get("Retry-After")
        if exc.code in (403, 429) and limited:
            raise HostError("GitHub rate limit reached; try again later.") from None
        if exc.code == 403:
            raise ValueError("GitHub refused the request (403): the token lacks permission.") from None
        raise HostError(f"GitHub answered HTTP {exc.code}.") from None
    except urllib.error.URLError as exc:
        raise HostError(scrub(f"Cannot reach GitHub: {exc.reason}", server)) from None
    except (TimeoutError, OSError) as exc:
        raise HostError(scrub(f"Cannot reach GitHub: {exc}", server)) from None


def request(server: dict, path: str, params: dict | None = None):
    """GET ``path`` (or a full URL) and return ``(json, next page url or "")``."""
    url = path if path.startswith("http") else api_base(server) + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    with _open(server, url) as resp:
        raw = resp.read()
        link = resp.headers.get("Link") or ""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise HostError("GitHub sent an unreadable answer.") from None
    found = _NEXT.search(link)
    return data, (found.group(1) if found else "")


def whoami(server: dict) -> dict:
    data, _ = request(server, "/user")
    return data


def list_repos(server: dict) -> tuple[list[dict], bool]:
    """Every repository the account can see, newest first: ``(repos, truncated)``."""
    repos: list[dict] = []
    url, params = "/user/repos", {"per_page": 100, "sort": "updated",
                                  "affiliation": "owner,collaborator,organization_member"}
    for _ in range(MAX_PAGES):
        data, url = request(server, url, params)
        params = None
        repos += [slim(r) for r in data if isinstance(r, dict)]
        if not url:
            return repos, False
    return repos, True


def slim(repo: dict) -> dict:
    return {"full_name": repo.get("full_name") or "", "name": repo.get("name") or "",
            "owner": (repo.get("owner") or {}).get("login") or "",
            "private": bool(repo.get("private")), "fork": bool(repo.get("fork")),
            "archived": bool(repo.get("archived")), "description": repo.get("description") or "",
            "language": repo.get("language") or "", "default_branch": repo.get("default_branch") or "",
            "updated_at": repo.get("updated_at") or "", "html_url": repo.get("html_url") or "",
            "ssh_url": repo.get("ssh_url") or "", "clone_url": repo.get("clone_url") or ""}


def clean_server(values: dict, old: dict | None) -> dict:
    api = (values.get("api_url") or "").strip().rstrip("/")
    if api and not re.match(r"^https?://[^/\s]+", api, re.IGNORECASE):
        raise ValueError("The API URL must start with http:// or https://.")
    env = (values.get("token_env") or "").strip()
    if env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env):
        raise ValueError("The environment variable name may only hold letters, digits and _.")
    return {"api_url": api, "token_env": env}


def test_server(server: dict) -> dict:
    return {"message": f"✓ Signed in as {whoami(server).get('login', '?')}"}
