"""SSH tools plugin: saved servers, keys, key copying, a login network diagram and port tunnels."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from flask import abort, jsonify, request

from patchgoblin.hosts import HostError, host_for
from patchgoblin.pluginkit import Plugin

from . import sshtools


def body() -> dict:
    return request.get_json(silent=True) or {}


def ssh_host(source: dict):
    """A host for a key picker value: "local"/empty is this computer (None)."""
    target = str(source.get("target") or "").strip()
    if not target or target == "local":
        return None
    os_name = source.get("os")
    return host_for({"location": "ssh", "ssh_target": target, "ssh_port": source.get("port") or None,
                     "ssh_os": os_name if os_name in ("posix", "windows")
                     else ("windows" if source.get("windows") else None)})


class SshPlugin(Plugin):
    id = "ssh"
    name = "SSH tools"
    description = "Saved servers, SSH keys, copying keys to hosts, a login network diagram and port tunnels."
    icon = '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>'
    order = 10

    def __init__(self, ctx):
        super().__init__(ctx)
        # Servers and tunnels were top-level settings before SSH tools became a plugin.
        ctx.settings.adopt_legacy(self.id, {"ssh_servers": "servers", "ssh_tunnels": "tunnels"})
        self.tunnels = sshtools.TunnelManager()

    def stop(self) -> None:
        self.tunnels.stop_all()

    # ---- saved data ----------------------------------------------------------------
    def servers(self) -> list[dict]:
        return sshtools.clean_servers(self.ctx.load().get("servers"))

    def saved_tunnels(self) -> list[dict]:
        return sshtools.clean_tunnels(self.ctx.load().get("tunnels"))

    def _save(self, key: str, value: list) -> None:
        self.ctx.save({**self.ctx.load(), key: value})

    def routes(self, bp) -> None:
        tunnels = self.tunnels

        def servers_view() -> dict:
            servers = self.servers()
            return {"servers": servers, "suggestions": sshtools.server_suggestions(self.ctx.registry, servers)}

        def saved_server(sid: str) -> dict:
            for server in self.servers():
                if server["id"] == sid:
                    return server
            abort(404)

        @bp.post("/servers/<sid>/wake")
        def wake_server(sid):
            server = saved_server(sid)
            if not server["mac"]:
                raise ValueError("Add the server's MAC address to wake it.")
            sshtools.send_wol(server["mac"], server["wol_address"])
            return jsonify(ok=True, mac=server["mac"], address=server["wol_address"] or "255.255.255.255")

        @bp.get("/servers")
        def get_servers():
            return jsonify(servers_view())

        @bp.put("/servers")
        def put_servers():
            self._save("servers", sshtools.clean_servers(body().get("servers"), strict=True))
            return jsonify(servers_view())

        @bp.get("/keys")
        def ssh_keys():
            port = request.args.get("port")
            host = ssh_host({"target": request.args.get("source", "local"), "port": int(port) if port else None,
                             "os": request.args.get("os"), "windows": request.args.get("windows") == "1"})
            return jsonify(keys=sshtools.list_local_keys() if host is None else sshtools.list_remote_keys(host))

        @bp.post("/network")
        def ssh_network():
            """Scan every saved server's authorized_keys once (only when asked: no polling)."""
            servers = self.servers()
            scans = {}
            if servers:
                pool = ThreadPoolExecutor(max_workers=min(8, len(servers)))
                futures = {s["id"]: pool.submit(sshtools.scan_server, ssh_host(
                    {"target": s["target"], "port": s["port"], "os": s["os"]})) for s in servers}
                deadline = time.monotonic() + 90
                for sid, future in futures.items():
                    try:
                        scans[sid] = future.result(timeout=max(1, deadline - time.monotonic()))
                    except Exception as exc:  # timeout or an unexpected failure: show it on the node
                        scans[sid] = {"files": [], "own_keys": [], "admin": False, "os": "",
                                      "error": str(exc) or "The scan timed out."}
                pool.shutdown(wait=False, cancel_futures=True)
            return jsonify(**sshtools.build_network(sshtools.list_local_keys(), servers, scans),
                           generated=time.strftime("%Y-%m-%d %H:%M:%S"))

        @bp.post("/keys/generate")
        def ssh_generate():
            data = body()
            port = data.get("port")
            host = ssh_host({"target": data.get("source"), "port": int(port) if port else None,
                             "os": data.get("os"), "windows": bool(data.get("windows"))})
            key = sshtools.generate_key(str(data.get("name") or ""), str(data.get("type") or "ed25519"),
                                        str(data.get("comment") or ""), str(data.get("passphrase") or ""), host)
            return jsonify(key=key), 201

        @bp.post("/install")
        def ssh_install():
            data = body()
            dests = data.get("targets")
            if not isinstance(dests, list) or not dests:
                raise ValueError("Add at least one destination.")
            keys = sshtools.clean_pubkeys(data.get("keys"))  # reject bad keys before touching any host
            results = []
            for dest in dests:
                dest = dest if isinstance(dest, dict) else {}
                target = str(dest.get("target") or "").strip()
                row = {"target": target, "ok": False, "added": [], "skipped": [], "note": "", "error": ""}
                try:
                    port = int(dest["port"]) if dest.get("port") else None
                    windows = bool(dest.get("windows"))
                    if data.get("interactive"):
                        row.update(sshtools.install_keys_interactive(target, port, keys, windows))
                    else:
                        host = ssh_host({"target": target, "port": port, "os": "windows" if windows else "posix"})
                        if host is None:
                            raise ValueError("Choose an SSH destination.")
                        row.update(sshtools.install_keys(host, keys))
                    row["ok"] = True
                except sshtools.AuthError as exc:
                    row["error"] = f'{exc} Retry with "Use password (opens terminal)".'
                except (HostError, ValueError) as exc:
                    row["error"] = str(exc)
                results.append(row)
            return jsonify(results=results)

        def tunnels_view() -> dict:
            status = tunnels.status()
            idle = {"state": "stopped", "returncode": None, "stderr": "", "started": 0}
            specs = [{**spec, **status.get(spec["id"], idle)} for spec in self.saved_tunnels()]
            return {"tunnels": specs, "running": tunnels.running_count()}

        @bp.get("/tunnels")
        def get_tunnels():
            return jsonify(tunnels_view())

        @bp.put("/tunnels")
        def put_tunnels():
            cleaned = sshtools.clean_tunnels(body().get("tunnels"), strict=True)
            old = {t["id"]: t for t in self.saved_tunnels()}
            kept = {s["id"] for s in cleaned}
            for tid, spec in old.items():
                if tunnels.is_running(tid) and (tid not in kept or next(s for s in cleaned if s["id"] == tid) != spec):
                    raise ValueError(f"Stop the tunnel {spec['name']!r} before changing or deleting it.")
            self._save("tunnels", cleaned)
            return jsonify(tunnels_view())

        def saved_tunnel(tid: str) -> dict:
            for spec in self.saved_tunnels():
                if spec["id"] == tid:
                    return spec
            abort(404)

        @bp.post("/tunnels/<tid>/start")
        def start_tunnel(tid):
            tunnels.start(saved_tunnel(tid))
            return jsonify(tunnels_view())

        @bp.post("/tunnels/<tid>/stop")
        def stop_tunnel(tid):
            saved_tunnel(tid)
            tunnels.stop(tid)
            return jsonify(tunnels_view())


PLUGIN = SshPlugin
