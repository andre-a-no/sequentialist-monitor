# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The HTTP API of the service: status, alarms, KPIs, and the commands of people.

    GET  /health                       the cycle, the sources, units that are down
    GET  /units                        every unit: running, active alarms, task, spec
    GET  /units/{unit}                 a unit: its features (last window, residual, detectors) and alarms
    GET  /alarms?state=&console=&priority=   the alarms that are not normal (or in the given ISA-18.2 state)
    GET  /kpi?console=&from=&to=       KPIs of alarm management (times: RFC 3339 or seconds)
    POST /units/{unit}/commands        {"command", "args", "actor", "reason"}: 202, 400, 401, 404, 409

Listens on 127.0.0.1 by default.  Commands need ``Authorization: Bearer
<token>``, the token taken from the environment variable the plant names;
without the variable set, commands are refused.  Use a reverse proxy for
TLS.  Reads serve the snapshot the service publishes after every cycle;
commands wait in its queue and run in its thread.
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from sequentialist.core.schema import parse_timestamp

from .alarms import NORMAL
from .kpi import kpi

logger = logging.getLogger("sequentialist.monitor")
MAX_BODY = 64 * 1024


def _time(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return parse_timestamp(text)


class Api:
    def __init__(self, service: Any, host: str, port: int, token: str | None):
        self.service, self.token = service, token
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "sequentialist-monitor"

            def log_message(self, format: str, *args: Any) -> None:
                logger.info("api %s " + format, self.client_address[0], *args)

            def _send(self, status: int, body: Any) -> None:
                data = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                try:
                    status, body = api.get(urlparse(self.path))
                except ValueError as error:
                    status, body = 400, {"error": str(error)}
                self._send(status, body)

            def do_POST(self) -> None:
                url = urlparse(self.path)
                parts = url.path.strip("/").split("/")
                if len(parts) != 3 or parts[0] != "units" or parts[2] != "commands":
                    return self._send(404, {"error": "not found"})
                given = self.headers.get("Authorization", "")
                if not api.token or not hmac.compare_digest(given.encode(), f"Bearer {api.token}".encode()):
                    return self._send(401, {"error": "commands need Authorization: Bearer <token>"})
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._send(413, {"error": "too large"})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    name, args = body["command"], body.get("args", {})
                    actor, reason = body["actor"], body["reason"]
                    if not (isinstance(name, str) and isinstance(args, dict) and isinstance(actor, str)
                            and isinstance(reason, str)):
                        raise TypeError
                except (ValueError, KeyError, TypeError):
                    return self._send(400, {"error": "expected {\"command\", \"args\", \"actor\", \"reason\"}"})
                pending = api.service.submit(parts[1], name, args, actor, reason)
                self._send(pending.status, pending.result)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="sequentialist-api", daemon=True)

    @property
    def address(self) -> tuple[str, int]:
        return self.server.server_address[:2]  # type: ignore[return-value]

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def get(self, url: Any) -> tuple[int, Any]:
        snap = self.service.snapshot
        units = snap.get("units", {})
        parts = url.path.strip("/").split("/")
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if url.path in ("/health", "/health/"):
            ok = not snap.get("down") and all(s["error"] is None for s in snap.get("sources", {}).values())
            return 200, {"ok": ok, "time": snap.get("time"), "cycles": snap.get("cycles"),
                         "sources": snap.get("sources"), "down": snap.get("down")}
        if parts == ["units"]:
            return 200, {uid: {"task": u["task"], "console": u["console"], "running": u["running"],
                               "watermark": u["watermark"], "down": u["down"],
                               "alarms": sum(1 for a in u["alarms"].values() if a["state"] != NORMAL)}
                         for uid, u in units.items()}
        if len(parts) == 2 and parts[0] == "units":
            if parts[1] not in units:
                return 404, {"error": f"no unit {parts[1]!r}"}
            return 200, units[parts[1]]
        if parts == ["alarms"]:
            out = []
            for uid, u in units.items():
                if "console" in query and u["console"] != query["console"]:
                    continue
                for key, a in u["alarms"].items():
                    wanted = a["state"] == query["state"] if "state" in query else a["state"] != NORMAL
                    if wanted and ("priority" not in query or a["priority"] == query["priority"]):
                        out.append({"unit": uid, "source": key, **a})
            out.sort(key=lambda a: (-a["severity"], a["active_since"] or 0.0, a["unit"], a["source"]))
            return 200, out
        if parts == ["kpi"]:
            end = _time(query["to"]) if "to" in query else (snap.get("time") or 0.0)
            start = _time(query["from"]) if "from" in query else end - 86400
            entries = []
            for u in units.values():
                entries += self.service.storage_audit(u["task"])
            alarms = self.service.plant.data["alarms"]
            budget = self.service.plant.data["budget"]
            found = kpi(entries, start, end, flood=alarms["flood"], stale_after=alarms["stale_after"],
                        budget_per_hour=budget["alarms_per_hour"] * budget["detector_share"])
            if "console" in query:
                found = {query["console"]: found.get(query["console"], {})}
            return 200, found
        return 404, {"error": "not found"}
