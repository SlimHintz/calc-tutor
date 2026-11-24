"""Sleep- and training-aware scheduling, from a personal running-data server (Strava + Garmin).

Evidence (see the review report): sleep loss before learning hurts encoding most, and studying
right after hard exercise is a poor time for new material. So:
- recovery day (short sleep, or yesterday was a race / long run, or today is race day)
  -> "reviews only; new topics on a rested day"
- race days, the day after a race, and long-run days are auto-protected: they never break the
  never-miss-twice chain.

Everything degrades gracefully: no token / server down -> no wellness data, nothing breaks.
"""

from __future__ import annotations

import sqlite3

import logging
import os
from datetime import date, timedelta

import httpx

from .db import get_setting, set_setting

log = logging.getLogger("calc_tutor.wellness")

LONG_RUN_M = 21_000      # half marathon or longer counts as a long run
SHORT_SLEEP_H = 6.5


def _api() -> tuple[str, str] | None:
    token = os.environ.get("RUNNING_API_TOKEN")
    if not token:
        return None
    return os.environ.get("RUNNING_API_URL", "http://localhost:8787").rstrip("/"), token


def _get(path: str, params: dict | None = None):
    api = _api()
    if not api:
        return None
    base, token = api
    try:
        r = httpx.get(f"{base}{path}", params=params, timeout=20,
                      headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("running api %s failed: %s", path, e)
        return None


def fetch(day: date) -> dict | None:
    """Pull sleep for `day` and runs from the last ~2 weeks."""
    if not _api():
        return None
    sleep = _get("/garmin/sleep", {"date": day.isoformat()}) or {}
    secs = (sleep.get("dailySleepDTO") or {}).get("sleepTimeSeconds")
    acts = _get("/strava/activities", {"per_page": 30}) or []
    runs = []
    for a in acts if isinstance(acts, list) else []:
        if (a.get("sport_type") or a.get("type")) not in ("Run", "TrailRun", "VirtualRun"):
            continue
        runs.append({"date": (a.get("start_date_local") or "")[:10],
                     "km": round((a.get("distance") or 0) / 1000, 1),
                     "race": a.get("workout_type") == 1,
                     "name": a.get("name")})
    return {"sleep_hours": round(secs / 3600, 2) if secs else None, "runs": runs}


def evaluate(day: date, data: dict) -> dict:
    yesterday = (day - timedelta(days=1)).isoformat()
    today = day.isoformat()
    reasons = []
    sh = data.get("sleep_hours")
    if sh is not None and sh < SHORT_SLEEP_H:
        reasons.append(f"short sleep ({sh:.1f} h)")
    for r in data.get("runs", []):
        if r["date"] == yesterday and r["race"]:
            reasons.append(f"raced yesterday ({r['name']})")
        elif r["date"] == yesterday and r["km"] * 1000 >= LONG_RUN_M:
            reasons.append(f"long run yesterday ({r['km']} km)")
        if r["date"] == today and r["race"]:
            reasons.append("race day 🏁")
    return {"date": today, "sleep_hours": sh, "recovery_day": bool(reasons), "reasons": reasons,
            "available": True}


def protect_from_runs(conn: sqlite3.Connection, data: dict) -> list[str]:
    """Protect race days, the day after a race, and long-run days."""
    from .engine import protect_day

    done = []
    for r in data.get("runs", []):
        if not r["date"]:
            continue
        d = date.fromisoformat(r["date"])
        if r["race"]:
            protect_day(conn, d.isoformat(), f"race: {r['name']}")
            protect_day(conn, (d + timedelta(days=1)).isoformat(), "post-race recovery")
            done += [d.isoformat(), (d + timedelta(days=1)).isoformat()]
        elif r["km"] * 1000 >= LONG_RUN_M:
            protect_day(conn, d.isoformat(), f"long run ({r['km']} km)")
            done.append(d.isoformat())
    return done


def refresh(conn: sqlite3.Connection, day: date | None = None) -> dict:
    """Fetch, evaluate and cache today's wellness; protect run days. Called by the scheduler."""
    from .engine import local_today

    day = day or local_today()
    data = fetch(day)
    if data is None:
        out = {"date": day.isoformat(), "available": False, "recovery_day": False, "reasons": []}
    else:
        out = evaluate(day, data)
        out["protected"] = protect_from_runs(conn, data)
    set_setting(conn, f"wellness:{day.isoformat()}", out)
    return out


def today(conn: sqlite3.Connection) -> dict:
    from .engine import local_today

    d = local_today().isoformat()
    w = get_setting(conn, f"wellness:{d}") or {"date": d, "available": False,
                                                "recovery_day": False, "reasons": []}
    if get_setting(conn, f"recovery_override:{d}"):
        w = {**w, "recovery_day": True, "reasons": w.get("reasons", []) + ["you marked it"]}
    return w
