"""Background loop. Evidence-tuned (see the review report): habits form around ONE consistent
cue, and notification volume causes fatigue. So:

- one study cue at your planned time, only if you haven't studied today (and the day isn't
  protected); the wording rotates; after a missed day it's a gentle "never miss twice".
- the morning digest only when something is actually due (or it's Monday: weekly fresh start).
- pop quiz: opt-in, at most one a day, in your chosen window, no time-limited bonus.
- training block: a notification when the 6-week block test is ready.
- wellness (sleep/runs) refreshed each morning.

Each tick is idempotent (per-day state lives in settings), so restarts never double-send.
"""

from __future__ import annotations

import sqlite3

import logging
import random
import threading
import time
from datetime import datetime, timedelta

from . import config, engine, notify, reading, studypack, vault, wellness
from .db import connect, get_setting, set_setting

log = logging.getLogger("calc_tutor.scheduler")

DIGEST_HOUR = 7.5
WELLNESS_HOUR = 6.0

CUES = [
    ("📐 Study time", "Reviews first, then one idea. 30 minutes is plenty."),
    ("🧠 Your planned session", "Small, consistent reps beat long cram sessions."),
    ("∫ Ready when you are", "Warm up with your due reviews, then try one problem before reading."),
    ("🏃 Like an easy run", "Not every session needs to be a workout. Just show up."),
]


def _hour(hhmm: str) -> float:
    h, m = (hhmm or "19:00").split(":")
    return int(h) + int(m) / 60


def send_pop_quiz(conn: sqlite3.Connection) -> int | None:
    qs = engine.pick_questions(conn, mode="mixed", n=1, auto_only=True)
    if not qs:
        return None
    q = qs[0]
    nid = engine.notify(conn, "popquiz", "⚡ One quick question",
                        engine._strip_tex(q["prompt"])[:300], question_id=q["id"])
    conn.execute("UPDATE notifications SET link=? WHERE id=?",
                 (f"{config.public_url()}/#/q/{q['id']}?n={nid}", nid))
    return nid


def tick(now: datetime | None = None) -> None:
    now = now or engine.local_now()
    today = now.date()
    t = today.isoformat()
    hour = now.hour + now.minute / 60
    with connect() as conn:
        if hour >= WELLNESS_HOUR and get_setting(conn, "wellness_refreshed") != t:
            set_setting(conn, "wellness_refreshed", t)
            try:
                wellness.refresh(conn, today)
            except Exception:
                log.exception("wellness refresh failed")
        reading.tick(conn)
        vault.tick(conn)
        if engine.setting(conn, "paused"):
            return
        studypack.daily(conn, now)
        started = conn.execute("SELECT COUNT(*) FROM progress WHERE status!='new'").fetchone()[0]
        cons = engine.consistency(conn)

        # Morning digest: only when there's something to do, or Monday's fresh start
        if hour >= DIGEST_HOUR and get_setting(conn, "digest_sent") != t:
            set_setting(conn, "digest_sent", t)
            due = engine.due_count(conn)
            monday = today.weekday() == 0
            if due or monday:
                w = wellness.today(conn)
                lines = []
                if monday:
                    last = conn.execute(
                        "SELECT COUNT(*) FROM study_days WHERE studied=1 AND local_date >= ? "
                        "AND local_date < ?", ((today - timedelta(days=7)).isoformat(), t)
                    ).fetchone()[0]
                    lines.append(f"🗓️ Fresh week. Last week: {last}/{cons['week']['target']} "
                                 "study days. Same target this week.")
                if due:
                    lines.append(f"🔁 {due} reviews due")
                if w.get("recovery_day"):
                    lines.append(f"🛌 Recovery day ({', '.join(w['reasons'])}): reviews only.")
                else:
                    nt = engine.next_topic(conn)
                    if nt.get("id"):
                        lines.append(f"📚 Next: {nt['title']}")
                b = engine.block_info(conn)
                if b["test_ready"]:
                    lines.append(f"🏁 Block {b['number']} test is ready.")
                engine.notify(conn, "digest", "☀️ Today's plan", "\n".join(lines),
                              link=config.public_url() or None)

        # The one study cue
        cue_at = _hour(engine.setting(conn, "study_time"))
        if (started and hour >= cue_at and get_setting(conn, "cue_sent") != t
                and not cons["studied_today"] and not cons["protected_today"]):
            set_setting(conn, "cue_sent", t)
            if cons["missed_yesterday"]:
                title, body = ("🔗 Never miss twice",
                               "Yesterday was a rest day. Ten minutes of reviews today keeps the "
                               "chain.")
            else:
                title, body = CUES[today.toordinal() % len(CUES)]
            engine.notify(conn, "nudge", title, body, link=f"{config.public_url()}/#/review")

        # Opt-in pop quiz: at most one, inside your window
        n = min(1, int(engine.setting(conn, "pop_quizzes_per_day") or 0))
        if started and n:
            plan = get_setting(conn, f"pop_plan:{t}")
            if plan is None:
                start, end = engine.setting(conn, "pop_window")
                base = datetime.fromisoformat(t).replace(tzinfo=config.tz())
                plan = (base + timedelta(minutes=random.randrange(start * 60, end * 60))).isoformat()
                set_setting(conn, f"pop_plan:{t}", plan)
            if datetime.fromisoformat(plan) <= now and not get_setting(conn, f"pop_sent:{t}"):
                set_setting(conn, f"pop_sent:{t}", True)
                send_pop_quiz(conn)

        # Training block test ready
        b = engine.block_info(conn)
        if b["test_ready"] and get_setting(conn, "block_notified") != b["number"]:
            set_setting(conn, "block_notified", b["number"])
            engine.notify(conn, "nudge", f"🏁 Block {b['number']} test ready",
                          "Six weeks done. Take the cumulative test (like a tune-up race).",
                          link=f"{config.public_url()}/#/quiz?mode=block")
    notify.push_pending()


def _loop() -> None:
    while True:
        try:
            tick()
        except Exception:
            log.exception("scheduler tick failed")
        time.sleep(60)


def start() -> None:
    threading.Thread(target=_loop, name="calc-tutor-scheduler", daemon=True).start()
