"""Pushes pending notifications to the phone via ntfy (https://ntfy.sh).

Every notification is stored in the DB first (the in-app inbox); pushing is
best-effort on top. If CALC_TUTOR_NTFY_URL is unset nothing leaves the Pi.
"""

from __future__ import annotations

import logging
import threading

import httpx

from . import config
from .db import connect, get_setting, rows

log = logging.getLogger("calc_tutor.notify")
_lock = threading.Lock()

TAGS = {"popquiz": "zap", "fact": "sparkles", "achievement": "trophy", "levelup": "arrow_up",
        "digest": "sunrise", "streak": "fire", "nudge": "books", "pack": "blue_book"}


def push_pending() -> int:
    url = config.ntfy_url()
    if not _lock.acquire(blocking=False):
        return 0
    sent = 0
    try:
        with connect() as conn:
            if not url or not get_setting(conn, "push_enabled", True):
                conn.execute("UPDATE notifications SET pushed=-1 WHERE pushed=0")
                return 0
            muted = set(get_setting(conn, "push_muted_kinds", []))
            pending = rows(conn.execute("SELECT * FROM notifications WHERE pushed=0 ORDER BY id"))
        for n in pending:
            ok = -1
            if n["kind"] not in muted:
                headers = {"Title": n["title"].encode("utf-8"),
                           "Tags": TAGS.get(n["kind"], "abacus")}
                link = n["link"] or config.public_url() or None
                if link:
                    headers["Click"] = link
                if n["kind"] == "popquiz":
                    headers["Priority"] = "high"
                try:
                    r = httpx.post(url, content=n["body"].encode("utf-8"), headers=headers,
                                   timeout=10)
                    ok = 1 if r.status_code < 300 else 0
                except httpx.HTTPError as e:
                    log.warning("ntfy push failed: %s", e)
                    ok = 0
            if ok == 0:
                break  # keep it pending; retry on next tick
            with connect() as conn:
                conn.execute("UPDATE notifications SET pushed=? WHERE id=?", (ok, n["id"]))
            sent += ok == 1
    finally:
        _lock.release()
    return sent


def kick() -> None:
    """Push in the background right away (called after events like topic completion)."""
    threading.Thread(target=push_pending, daemon=True).start()
