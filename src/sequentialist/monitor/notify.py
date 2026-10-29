# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Who is told about an alarm: the plant's channels (log, JSON lines, webhook, e-mail), filtered.

The audit log records every transition; a channel announces only those whose
event says ``notify`` (an alarm became active and was not shelved,
suppressed, out of service or chattering; it returned; a person acted) and
whose priority the channel takes.  The engine hands entries to sinks after
the checkpoint that made them durable: an announcement is never made of a
decision that a crash could undo, and a crash between the checkpoint and the
sending loses the announcement, not the entry (sequentialist-core).

The body of a webhook is the audit entry as JSON: its `basis` holds the
fields of an OPC UA Alarms & Conditions event, its `n` and `hash` identify
the entry in the task's log.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import smtplib
import threading
import time
from email.message import EmailMessage
from typing import Any, Callable, Mapping, Sequence

from sequentialist.core import AuditEntry, JsonlSink, LogSink, WebhookSink

logger = logging.getLogger("sequentialist.monitor")

Sink = Callable[[AuditEntry], None]


class Announce:
    """Passes to `sink` the entries that notify, of the given priorities (and consoles, if given)."""

    def __init__(self, sink: Sink, priorities: Sequence[str], consoles: Sequence[str] | None = None):
        self.sink, self.priorities = sink, tuple(priorities)
        self.consoles = tuple(consoles) if consoles is not None else None

    def __call__(self, entry: AuditEntry) -> None:
        b = entry.basis
        if entry.kind not in ("decision", "action") or not b.get("notify"):
            return
        if b.get("priority") not in self.priorities:
            return
        if self.consoles is not None and b.get("console") not in self.consoles:
            return
        self.sink(entry)

    def close(self) -> None:
        close = getattr(self.sink, "close", None)
        if close is not None:
            close()


Send = Callable[[EmailMessage], None]


class EmailSink:
    """Sends an entry as an e-mail through SMTP from a background thread, with retries."""

    def __init__(self, host: str, port: int, sender: str, to: Sequence[str], *, starttls: bool = False,
                 user: str | None = None, password: str | None = None, timeout: float = 10.0, attempts: int = 5,
                 send: Send | None = None):
        self.host, self.port, self.sender, self.to = host, port, sender, list(to)
        self.starttls, self.user, self.password = starttls, user, password
        self.timeout, self.attempts = timeout, attempts
        self._send = send or self._smtp
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._work, name="sequentialist-email", daemon=True)
        self._thread.start()

    def __call__(self, entry: AuditEntry) -> None:
        self._queue.put(entry)

    def message(self, entry: AuditEntry) -> EmailMessage:
        b = entry.basis
        m = EmailMessage()
        m["Subject"] = f"[{b.get('priority', '?')}] {b.get('message', entry.decision)}"
        m["From"] = self.sender
        m["To"] = ", ".join(self.to)
        lines = [b.get("message", ""), "",
                 f"Source: {b.get('source_name')} / {b.get('condition')}",
                 f"Transition: {b.get('transition')} ({b.get('state', {}).get('before')} -> "
                 f"{b.get('state', {}).get('after')})",
                 f"Priority: {b.get('priority')} (severity {b.get('severity')})"]
        if entry.actor:
            lines.append(f"By: {entry.actor} ({entry.reason})")
        if "guarantee" in b:
            lines.append(f"Guarantee: {b['guarantee']}")
        lines += ["", "An advisory system: not a safety instrumented system (no claim under IEC 61511).", "",
                  json.dumps(entry.to_json(), indent=2, ensure_ascii=False)]
        m.set_content("\n".join(lines))
        return m

    def _smtp(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password or "")
            smtp.send_message(message)

    def _work(self) -> None:
        while True:
            entry = self._queue.get()
            if entry is None:
                return
            for attempt in range(self.attempts):
                try:
                    self._send(self.message(entry))
                    break
                except Exception as error:
                    if attempt + 1 == self.attempts:
                        logger.error("e-mail to %s: entry %d of task %s not delivered: %s", self.to, entry.n,
                                     entry.task_id, error)
                    else:
                        time.sleep(min(60.0, 0.5 * 2**attempt))

    def close(self, timeout: float = 10.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)


def plant_sinks(notify: Sequence[Mapping[str, Any]], environ: Mapping[str, str] | None = None) -> list[Any]:
    """The sinks of a plant's `notify` list; secrets come from the environment."""
    env = os.environ if environ is None else environ
    out: list[Any] = []
    for n in notify:
        kind = n["type"]
        if kind == "log":
            out.append(LogSink(kinds=n["kinds"], logger_name="sequentialist.monitor"))
        elif kind == "jsonl":
            out.append(JsonlSink(n["path"]))
        elif kind == "webhook":
            url = env.get(n["url_env"])
            if not url:
                raise ValueError(f"the environment variable {n['url_env']} (a webhook's URL) is not set")
            out.append(Announce(WebhookSink(url, timeout=n["timeout"]), n["priorities"]))
        elif kind == "email":
            host, _, port = n["smtp"].partition(":")
            user = env.get(n["user_env"]) if n["user_env"] else None
            password = env.get(n["password_env"]) if n["password_env"] else None
            out.append(Announce(EmailSink(host, int(port or 25), n["sender"], n["to"], starttls=n["starttls"],
                                          user=user, password=password), n["priorities"]))
        else:
            raise ValueError(f"unknown channel {kind!r}")
    return out
