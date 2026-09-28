"""What PatchGoblin needs to know about a project's opencode setup: the models and agents
its opencode config defines. Files are read through the project's host, so SSH projects
see the remote user's config.

opencode also merges configs from parent directories, ``OPENCODE_CONFIG_DIR`` and remote
sources; those are not read here, so model lists are suggestions, not the full set.
"""
from __future__ import annotations

import json
import os
import re

# Agents opencode ships with; custom ones come from opencode.json or agent/*.md files.
BUILTIN_AGENTS = frozenset({"build", "plan", "general", "explore"})
CONFIG_NAMES = ("opencode.json", "opencode.jsonc")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_MODEL_LINE = re.compile(r"^\S+/\S+$")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text or "")


def strip_jsonc(text: str) -> str:
    """JSON with comments → JSON: drops ``//`` and ``/* */`` comments and trailing commas
    outside strings."""
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            in_str = c != '"'
            i += 1
            continue
        if c == '"':
            in_str = True
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        elif c in "}]":
            # Outside a string, a comma before only whitespace is a trailing comma.
            j = len(out) - 1
            while j >= 0 and out[j].isspace():
                j -= 1
            if j >= 0 and out[j] == ",":
                del out[j]
        out.append(c)
        i += 1
    return "".join(out)


def _config_dirs(host) -> list[str]:
    """The user-level opencode config directories on the project's host."""
    dirs = [host.join(host.home(), ".config", "opencode")]
    if host.kind == "local" and os.environ.get("OPENCODE_CONFIG_DIR"):
        dirs.append(os.environ["OPENCODE_CONFIG_DIR"])
    return dirs


def config_paths(host, cwd: str) -> list[str]:
    """opencode config files in the order opencode applies them (later ones win)."""
    paths = []
    if host.kind == "local" and os.environ.get("OPENCODE_CONFIG"):
        paths.append(os.environ["OPENCODE_CONFIG"])
    for d in _config_dirs(host):
        paths += [host.join(d, name) for name in CONFIG_NAMES]
    paths += [host.join(cwd, name) for name in CONFIG_NAMES]
    paths.append(host.join(cwd, ".opencode", "opencode.json"))
    return paths


def read_configs(host, cwd: str) -> tuple[list[dict], list[str]]:
    """The opencode configs that exist, parsed, and a warning for each that can't be."""
    configs, warnings = [], []
    for path in config_paths(host, cwd):
        text = host.read_text(path)
        if text is None:
            continue
        try:
            data = json.loads(strip_jsonc(text.lstrip("﻿")))
        except ValueError as exc:
            warnings.append(f"{path} is not valid JSON: {exc}")
            continue
        if not isinstance(data, dict):
            warnings.append(f"{path} is not a JSON object.")
            continue
        configs.append(data)
    return configs, warnings


def _name(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def config_models(configs: list[dict]) -> list[str]:
    """Model ids (``provider/model``) the configs mention, in order, without duplicates."""
    found = []
    for cfg in configs:
        found += [_name(cfg.get("model")), _name(cfg.get("small_model"))]
        agents = cfg.get("agent")
        if isinstance(agents, dict):
            found += [_name(a.get("model")) for a in agents.values() if isinstance(a, dict)]
        providers = cfg.get("provider")
        if isinstance(providers, dict):
            for pid, provider in providers.items():
                models = provider.get("models") if isinstance(provider, dict) else None
                if isinstance(models, dict):
                    found += [f"{pid}/{mid}" for mid in models]
    return list(dict.fromkeys(m for m in found if m))


def cli_models(host, cwd: str, timeout: float = 30) -> list[str]:
    """The models ``opencode models`` lists (every provider opencode has credentials for)."""
    res = host.run(["opencode", "models"], cwd=cwd, timeout=timeout, login=True, env={"NO_COLOR": "1"})
    if res.timed_out:
        raise RuntimeError(f"opencode models timed out after {int(timeout)}s.")
    if res.returncode != 0:
        detail = strip_ansi(res.stderr or res.stdout).strip()[-500:]
        raise RuntimeError(f"opencode models exited with code {res.returncode}: {detail}")
    lines = (line.strip() for line in strip_ansi(res.stdout).splitlines())
    return list(dict.fromkeys(line for line in lines if _MODEL_LINE.match(line)))


def agent_names(host, cwd: str, configs: list[dict]) -> set[str]:
    """Custom agents: the ``agent`` keys of the configs and the ``agent[s]/*.md`` files."""
    names = set()
    for cfg in configs:
        if isinstance(cfg.get("agent"), dict):
            names.update(cfg["agent"])
    dirs = [host.join(cwd, ".opencode")] + _config_dirs(host)
    for d in dirs:
        for sub in ("agent", "agents"):
            names.update(f[:-3] for f in host.list_dir(host.join(d, sub)) if f.endswith(".md"))
    return names
