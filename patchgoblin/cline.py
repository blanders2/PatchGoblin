"""Cline's model catalog. Cline has no "list models" command, but its npm package bundles a
catalog in ``@cline/llms`` (checked with cline 3.0.67 / @cline/llms 0.0.89). That is an internal
package, so every failure here is reported as a RuntimeError and callers fall back to the saved
suggestions.

The script runs on the project's host. It reads only ``lastUsedProvider`` from Cline's
providers.json; the API keys in that file are never read out.
"""
from __future__ import annotations

import json

from .opencode import strip_ansi

CATALOG_SCRIPT = r"""
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execSync } from "node:child_process";
import { pathToFileURL } from "node:url";

let provider = "cline";
try {
  const file = path.join(os.homedir(), ".cline", "data", "settings", "providers.json");
  const last = JSON.parse(fs.readFileSync(file, "utf8")).lastUsedProvider;
  if (typeof last === "string" && last) provider = last;
} catch {}

const root = execSync("npm root -g", { encoding: "utf8" }).trim();
const found = [
  path.join(root, "cline", "node_modules", "@cline", "llms", "dist", "index.js"),
  path.join(root, "@cline", "llms", "dist", "index.js"),
].find(p => fs.existsSync(p));
if (!found) throw new Error("@cline/llms not found under " + root);
const mod = await import(pathToFileURL(found).href);
if (typeof mod.getGeneratedModelsForProvider !== "function") {
  throw new Error("@cline/llms has no getGeneratedModelsForProvider export");
}
const models = await mod.getGeneratedModelsForProvider(provider);
console.log(JSON.stringify({ provider, models: Object.keys(models || {}) }));
"""


def catalog_models(host, cwd: str, timeout: float = 20) -> tuple[str, list[str]]:
    """(active provider, model ids) from Cline's bundled catalog on the project's host."""
    res = host.run(["node", "--input-type=module", "-"], cwd=cwd, input=CATALOG_SCRIPT,
                   timeout=timeout, login=True)
    if res.timed_out:
        raise RuntimeError(f"Reading Cline's model catalog timed out after {int(timeout)}s.")
    if res.returncode != 0:
        detail = strip_ansi(res.stderr or res.stdout).strip()[-500:]
        raise RuntimeError(f"Reading Cline's model catalog failed (exit {res.returncode}): {detail}")
    lines = strip_ansi(res.stdout).strip().splitlines()
    try:
        data = json.loads(lines[-1])
        provider, models = data["provider"], data["models"]
        if not isinstance(provider, str) or not isinstance(models, list) \
                or not all(isinstance(m, str) for m in models):
            raise ValueError
    except (IndexError, ValueError, KeyError, TypeError):
        raise RuntimeError("Cline's model catalog returned something unexpected.") from None
    return provider, list(dict.fromkeys(models))
