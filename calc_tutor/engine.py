"""Learning engine: XP/levels, consistency, mastery gate, FSRS spaced repetition,
tutor guardrails (hint ladder), interleaving, mistakes, placement, achievements.

Design notes (see reports/Math self study approach review.md for the evidence):
- "Completed" is provisional; "mastered" needs unaided, delayed evidence and decays with
  retrievability.
- XP favours quality (unaided first-try, delayed recall, mastery, fixing mistakes) over volume.
- Consistency is a weekly target + "never miss twice" chain, with protected rest/race days.
- Hints and solutions unlock only after an attempt; their use is logged as a dependence metric.

Every function takes an open sqlite3 connection; callers own the transaction.
"""

from __future__ import annotations

import sqlite3

import json
import random
from datetime import date, datetime, timedelta

from fsrs import Card as FsrsCard
from fsrs import Rating, Scheduler

from . import config, grading
from .db import get_setting, iso, one, parse_iso, rows, set_setting, utcnow

# --- Levels --------------------------------------------------------------------

LEVEL_TITLES = [
    "Curious Novice", "Function Wrangler", "Limit Seeker", "Epsilon Hunter", "Tangent Tamer",
    "Chain Ruler", "Slope Surfer", "Optimizer", "Riemann Summoner", "Integral Artisan",
    "Series Sorcerer", "Taylor's Apprentice", "Gradient Climber", "Jacobian Juggler",
    "Hessian Whisperer", "Backprop Bard", "Lagrange Strategist", "Gaussian Integrator",
    "Euler's Heir", "Newton–Leibniz Fellow", "Calculus Grandmaster",
]

UNAIDED_CHANNELS = ("ui", "mixed", "review", "pop", "block", "pretest")
DELAYED_CHANNELS = ("mixed", "review", "pop", "block")
HINT_WINDOW = timedelta(hours=12)

DEFAULT_SETTINGS = {
    "weekly_target": 5,          # study days per week
    "study_time": "19:00",       # the one daily cue, sent only if not studied yet
    "pop_quizzes_per_day": 0,    # opt-in, max 1
    "pop_window": [12, 18],
    "new_cards_per_day": 12,
    "review_cap": 60,            # ~25 min of reviews
    "backlog_limit": 30,         # due reviews above this: no new topics
    "retention_concept": 0.90,
    "retention_problem": 0.85,
    "mastery_ewma": 0.90,
    "paused": False,
    "push_enabled": True,
    "remarkable_packs": True,    # daily study pack to the reMarkable
    "remarkable_pack_hour": 5,   # sent the first time the device is awake after this hour
    "pack_pages": 12,            # book pages per study pack part (~30 min of reading)
}


def setting(conn: sqlite3.Connection, key):
    return get_setting(conn, key, DEFAULT_SETTINGS.get(key))


def level_threshold(level: int) -> int:
    """Cumulative XP needed to reach `level` (level 1 = 0 XP)."""
    return 50 * (level - 1) * level


def level_info(xp: int) -> dict:
    level = 1
    while xp >= level_threshold(level + 1):
        level += 1
    lo, hi = level_threshold(level), level_threshold(level + 1)
    return {
        "level": level,
        "title": LEVEL_TITLES[min(level - 1, len(LEVEL_TITLES) - 1)],
        "xp": xp,
        "level_floor": lo,
        "next_level_at": hi,
        "progress": (xp - lo) / (hi - lo),
    }


def total_xp(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount), 0) FROM xp_events").fetchone()[0]


def local_today() -> date:
    return datetime.now(config.tz()).date()


def local_now() -> datetime:
    return datetime.now(config.tz())


def award_xp(conn: sqlite3.Connection, amount: int, reason: str, topic_id: str | None = None) -> dict:
    before = level_info(total_xp(conn))
    conn.execute(
        "INSERT INTO xp_events(amount, reason, topic_id, ts, local_date) VALUES(?,?,?,?,?)",
        (amount, reason, topic_id, iso(), local_today().isoformat()),
    )
    after = level_info(total_xp(conn))
    out = {"xp": amount, "reason": reason, "total": after["xp"], "level": after["level"]}
    if after["level"] > before["level"]:
        out["level_up"] = after
        notify(conn, "levelup", f"⬆️ Level {after['level']}: {after['title']}!",
               f"You've earned {after['xp']} XP total. Next title at {after['next_level_at']} XP.")
    return out


# --- Consistency: weekly target + "never miss twice" ------------------------------


def mark_studied(conn: sqlite3.Connection) -> dict | None:
    """Record today as a study day. Returns a comeback/weekly-target award if earned."""
    today = local_today()
    t = today.isoformat()
    r = conn.execute("SELECT studied FROM study_days WHERE local_date=?", (t,)).fetchone()
    if r and r[0]:
        return None
    conn.execute("INSERT INTO study_days(local_date, studied) VALUES(?,1) "
                 "ON CONFLICT(local_date) DO UPDATE SET studied=1", (t,))
    out = None
    y = (today - timedelta(days=1)).isoformat()
    yr = conn.execute("SELECT studied, protected FROM study_days WHERE local_date=?",
                      (y,)).fetchone()
    any_before = conn.execute("SELECT COUNT(*) FROM study_days WHERE studied=1 AND local_date<?",
                              (t,)).fetchone()[0]
    if any_before and not (yr and (yr[0] or yr[1])):
        out = award_xp(conn, 15, "comeback after a missed day 💪")
    week = consistency(conn)["week"]
    if week["days"] == week["target"]:
        out = award_xp(conn, 30, f"weekly target hit ({week['target']} days)")
        set_setting(conn, "weeks_hit", (get_setting(conn, "weeks_hit", 0) or 0) + 1)
    return out


def protect_day(conn: sqlite3.Connection, day: str | None = None, reason: str = "rest day") -> dict:
    d = day or local_today().isoformat()
    date.fromisoformat(d)
    conn.execute("INSERT INTO study_days(local_date, protected, reason) VALUES(?,1,?) "
                 "ON CONFLICT(local_date) DO UPDATE SET protected=1, reason=excluded.reason",
                 (d, reason))
    return {"protected": d, "reason": reason}


def consistency(conn: sqlite3.Connection) -> dict:
    days = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT local_date, studied, protected, reason FROM study_days")}
    studied = {d for d, v in days.items() if v[0]}
    protected = {d for d, v in days.items() if v[1]}
    today = local_today()
    first = min((date.fromisoformat(d) for d in studied), default=today)

    def ok(d: date) -> bool:
        return d.isoformat() in studied or d.isoformat() in protected

    # chain: walk back from yesterday; break at two consecutive unprotected misses
    chain, misses, d = 0, 0, today - timedelta(days=1)
    while d >= first:
        if ok(d):
            misses = 0
        else:
            misses += 1
            if misses == 2:
                chain -= 1  # the first of the two misses isn't part of the chain
                break
        chain += 1
        d -= timedelta(days=1)
    if today.isoformat() in studied:
        chain += 1
    best = max(chain, get_setting(conn, "best_chain", 0) or 0)
    if best > (get_setting(conn, "best_chain", 0) or 0):
        set_setting(conn, "best_chain", best)
    monday = today - timedelta(days=today.weekday())
    week_days = sum(1 for i in range(7) if (monday + timedelta(days=i)).isoformat() in studied)
    yesterday = today - timedelta(days=1)
    return {
        "chain": max(chain, 0),
        "best_chain": best,
        "studied_today": today.isoformat() in studied,
        "protected_today": today.isoformat() in protected,
        "missed_yesterday": yesterday >= first and not ok(yesterday)
        and today.isoformat() not in studied,
        "lifetime_days": len(studied),
        "week": {"days": week_days, "target": setting(conn, "weekly_target"),
                 "start": monday.isoformat()},
    }


def streak(conn: sqlite3.Connection) -> dict:
    """Back-compat shim for older callers: maps to the never-miss-twice chain."""
    c = consistency(conn)
    return {"current": c["chain"], "best": c["best_chain"], "active_today": c["studied_today"]}


# --- Notifications (pushed by notify.py) ---------------------------------------


def notify(conn: sqlite3.Connection, kind: str, title: str, body: str, link: str | None = None,
           question_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO notifications(ts, kind, title, body, link, question_id) VALUES(?,?,?,?,?,?)",
        (iso(), kind, title, body, link, question_id))
    return cur.lastrowid


# --- FSRS spaced repetition --------------------------------------------------------

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4
_RATING = {1: Rating.Again, 2: Rating.Hard, 3: Rating.Good, 4: Rating.Easy}
_schedulers: dict[float, Scheduler] = {}


def _scheduler(retention: float) -> Scheduler:
    if retention not in _schedulers:
        _schedulers[retention] = Scheduler(desired_retention=retention)
    return _schedulers[retention]


def _retention_for(conn: sqlite3.Connection, card: dict) -> float:
    return setting(conn, "retention_problem" if card.get("question_id")
                   else "retention_concept")


def _fsrs_card(card: dict) -> FsrsCard:
    if card.get("fsrs"):
        return FsrsCard.from_dict(json.loads(card["fsrs"]))
    c = FsrsCard()
    if card.get("reps"):  # migrate an SM-2 card that already has history
        d = c.to_dict()
        d.update(state=2, step=None, stability=max(float(card.get("interval") or 1), 0.5),
                 difficulty=5.0, due=card["due"], last_review=card.get("last_review"))
        c = FsrsCard.from_dict(d)
    elif card.get("due"):
        d = c.to_dict()
        d["due"] = parse_iso(card["due"]).isoformat()
        c = FsrsCard.from_dict(d)
    return c


def retrievability(conn: sqlite3.Connection, card: dict, now: datetime | None = None) -> float:
    if not card.get("last_review"):
        return 0.0
    c = _fsrs_card(card)
    return float(_scheduler(_retention_for(conn, card)).get_card_retrievability(c, now or utcnow()))


def apply_review(conn: sqlite3.Connection, card_id: int, rating: int) -> dict:
    card = one(conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)))
    if not card:
        raise ValueError(f"no card {card_id}")
    now = utcnow()
    elapsed = (now - parse_iso(card["last_review"])).days if card["last_review"] else 0
    c = _fsrs_card(card)
    was_review = c.to_dict()["state"] == 2
    c, _ = _scheduler(_retention_for(conn, card)).review_card(c, _RATING[rating], now)
    d = c.to_dict()
    due = parse_iso(d["due"])
    interval = round((due - now).total_seconds() / 86400, 2)
    lapses = card["lapses"] + (1 if rating == AGAIN and was_review else 0)
    conn.execute(
        "UPDATE cards SET fsrs=?, interval=?, reps=reps+1, lapses=?, due=?, last_review=?, "
        "active=1, first_review=COALESCE(first_review, ?), priority=? WHERE id=?",
        (json.dumps(d), interval, lapses, iso(due), iso(now), iso(now),
         card["priority"] if rating == AGAIN else 0, card_id))
    conn.execute("INSERT INTO reviews(card_id, rating, ts) VALUES(?,?,?)", (card_id, rating, iso()))
    return {"card_id": card_id, "due": iso(due), "interval_days": interval,
            "stability": d["stability"], "elapsed_days": elapsed}


def _activate_card(conn: sqlite3.Connection, card_id: int, due: datetime | None = None) -> None:
    """Make a card schedulable as a NEW card (FSRS learning steps start at first review)."""
    card = one(conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)))
    if card["active"] and card["fsrs"]:
        return
    c = FsrsCard()
    d = c.to_dict()
    d["due"] = (due or utcnow()).isoformat()
    conn.execute("UPDATE cards SET active=1, fsrs=?, due=? WHERE id=?",
                 (json.dumps(d), iso(due or utcnow()), card_id))


def review_card(conn: sqlite3.Connection, card_id: int, rating: int) -> dict:
    """Self-graded flashcard review (1 again, 2 hard, 3 good, 4 easy)."""
    if rating not in (1, 2, 3, 4):
        raise ValueError("rating must be 1-4")
    out = apply_review(conn, card_id, rating)
    card = one(conn.execute("SELECT topic_id, kind FROM cards WHERE id=?", (card_id,)))
    xp = {1: 1, 2: 2, 3: 3, 4: 3}[rating]
    reason = "review"
    if rating >= HARD and out["elapsed_days"] >= 3:
        xp, reason = xp + 4, "delayed recall 🧠"
    out["award"] = award_xp(conn, xp, reason, card["topic_id"])
    mark_studied(conn)
    _touch(conn, card["topic_id"])
    out["mastery"] = check_mastery(conn, card["topic_id"])
    out["achievements"] = check_achievements(conn)
    return out


def _reviews_today(conn: sqlite3.Connection) -> tuple[int, int]:
    start = datetime.combine(local_today(), datetime.min.time(), tzinfo=config.tz())
    s = iso(start.astimezone(utcnow().tzinfo))
    reviews = conn.execute("SELECT COUNT(*) FROM reviews WHERE ts >= ?", (s,)).fetchone()[0]
    new = conn.execute("SELECT COUNT(*) FROM cards WHERE first_review >= ?", (s,)).fetchone()[0]
    return reviews, new


def due_cards(conn: sqlite3.Connection, limit: int = 20, topic_id: str | None = None) -> list[dict]:
    """Due review cards first (confident misses first), then new cards up to the daily new-card
    limit; total capped by the daily review cap. Problem cards show a fresh sibling problem."""
    done, new_done = _reviews_today(conn)
    room = max(0, setting(conn, "review_cap") - done)
    limit = min(limit, room)
    if limit <= 0:
        return []
    base = ("SELECT c.id, c.topic_id, c.question_id, c.front, c.back, c.reps, c.lapses, c.due, "
            "c.kind, c.priority, c.last_review, t.title AS topic_title FROM cards c "
            "JOIN topics t ON t.id=c.topic_id WHERE c.active=1 AND c.due <= ?")
    args: list = [iso()]
    if topic_id:
        base += " AND c.topic_id=?"
        args.append(topic_id)
    seen = rows(conn.execute(base + " AND c.last_review IS NOT NULL "
                             "ORDER BY c.priority DESC, c.due LIMIT ?", args + [limit]))
    new_room = max(0, min(limit - len(seen), setting(conn, "new_cards_per_day") - new_done))
    fresh = rows(conn.execute(base + " AND c.last_review IS NULL ORDER BY c.priority DESC, "
                              "c.due LIMIT ?", args + [new_room])) if new_room else []
    cards = seen + fresh
    for c in cards:
        if c["question_id"]:
            c["question"] = public_question(conn, _variant_for(conn, c))
    return cards


def _variant_for(conn: sqlite3.Connection, card: dict) -> int:
    """A different problem of the same kind on each review (so you practise the method, not
    the remembered answer): the least-recently-attempted sibling in the topic."""
    q = one(conn.execute("SELECT type FROM questions WHERE id=?", (card["question_id"],)))
    sibs = rows(conn.execute(
        "SELECT q.id, MAX(a.ts) AS last FROM questions q LEFT JOIN attempts a "
        "ON a.question_id=q.id WHERE q.topic_id=? AND q.active=1 AND q.type != 'free' "
        "AND q.id != ? GROUP BY q.id ORDER BY last IS NOT NULL, last LIMIT 1",
        (card["topic_id"], card["question_id"])))
    if card["reps"] and sibs and q:
        return sibs[0]["id"]
    return card["question_id"]


def due_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM cards WHERE active=1 AND due <= ? "
                        "AND last_review IS NOT NULL", (iso(),)).fetchone()[0]


def new_due_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM cards WHERE active=1 AND due <= ? "
                        "AND last_review IS NULL", (iso(),)).fetchone()[0]


def _ensure_question_card(conn: sqlite3.Connection, question_id: int, activate: bool) -> int:
    key = f"q:{question_id}"
    r = conn.execute("SELECT id, active FROM cards WHERE key=?", (key,)).fetchone()
    if r:
        if activate and not r["active"]:
            _activate_card(conn, r["id"])
        return r["id"]
    q = one(conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)))
    cur = conn.execute(
        "INSERT INTO cards(key, topic_id, question_id, front, back, active, due, created_at, kind) "
        "VALUES(?,?,?,?,?,0,?,?,'question')",
        (key, q["topic_id"], question_id, q["prompt"], answer_display(q), iso(), iso()))
    if activate:
        _activate_card(conn, cur.lastrowid)
    return cur.lastrowid


# --- Mastery: provisional completion vs evidence-based mastery ----------------------


def _ewma(seq: list[int]) -> float:
    e = 0.5
    for c in seq:
        e = 0.65 * e + 0.35 * c
    return e


def _mastery_from(attempts: list[int], retention: list[float], grades: list[float],
                  status: str) -> float:
    parts = []
    if attempts:
        parts.append((0.5, _ewma(attempts) * min(1.0, len(attempts) / 6)))
    if retention:
        parts.append((0.3, sum(retention) / len(retention)))
    if grades:
        parts.append((0.2, sum(grades[-3:]) / len(grades[-3:]) / 100))
    computed = sum(w * v for w, v in parts) / sum(w for w, _ in parts) if parts else 0.0
    floor = {"learning": 0.1, "completed": 0.25}.get(status, 0.0)
    return round(max(floor, computed), 3)


def mastery_map(conn: sqlite3.Connection) -> dict[str, float]:
    """Mastery 0..1 for every topic. Retention = current FSRS retrievability, so mastery
    decays when you stop reviewing."""
    atts: dict[str, list[int]] = {}
    for r in conn.execute("SELECT topic_id, correct FROM attempts WHERE correct IS NOT NULL "
                          "ORDER BY ts, id"):
        atts.setdefault(r[0], []).append(r[1])
    ret: dict[str, list[float]] = {}
    now = utcnow()
    for c in rows(conn.execute("SELECT * FROM cards WHERE active=1 AND last_review IS NOT NULL")):
        ret.setdefault(c["topic_id"], []).append(retrievability(conn, c, now))
    grades: dict[str, list[float]] = {}
    for r in conn.execute("SELECT topic_id, score FROM grades ORDER BY ts"):
        grades.setdefault(r[0], []).append(r[1])
    return {r[0]: _mastery_from(atts.get(r[0], []), ret.get(r[0], []), grades.get(r[0], []), r[1])
            for r in conn.execute("SELECT topic_id, status FROM progress")}


def mastery_evidence(conn: sqlite3.Connection, topic_id: str, mastery: float | None = None) -> dict:
    """The three conditions for 'mastered' and whether each is met."""
    p = one(conn.execute("SELECT status, completed_at FROM progress WHERE topic_id=?",
                         (topic_id,)))
    m = mastery if mastery is not None else mastery_map(conn).get(topic_id, 0)
    evidence_channels = tuple(ch for ch in UNAIDED_CHANNELS if ch != "pretest")
    unaided = rows(conn.execute(
        f"SELECT question_id, ts, channel FROM attempts WHERE topic_id=? AND correct=1 "
        f"AND aided=0 AND hints=0 AND revealed=0 AND channel IN "
        f"({','.join('?' * len(evidence_channels))})", (topic_id, *evidence_channels)))
    distinct = len({a["question_id"] for a in unaided})
    delayed = False
    if p and p["completed_at"]:
        cutoff = parse_iso(p["completed_at"]) + timedelta(days=3)
        delayed = any(a["channel"] in DELAYED_CHANNELS or parse_iso(a["ts"]) >= cutoff
                      for a in unaided if parse_iso(a["ts"]) >= parse_iso(p["completed_at"]))
    need = get_setting(conn, "mastery_ewma", DEFAULT_SETTINGS["mastery_ewma"])
    checks = {"completed": bool(p and p["status"] == "completed"),
              "mastery_score": {"value": m, "need": need, "ok": m >= need},
              "unaided_distinct_correct": {"value": distinct, "need": 3, "ok": distinct >= 3},
              "delayed_or_interleaved_success": delayed}
    checks["mastered"] = (checks["completed"] and checks["mastery_score"]["ok"]
                          and checks["unaided_distinct_correct"]["ok"] and delayed)
    return checks


def mastered_set(conn: sqlite3.Connection, m: dict[str, float] | None = None) -> set[str]:
    m = m if m is not None else mastery_map(conn)
    return {t for t in (r[0] for r in conn.execute(
        "SELECT topic_id FROM progress WHERE status='completed'"))
        if m.get(t, 0) >= setting(conn, "mastery_ewma")
        and mastery_evidence(conn, t, m.get(t))["mastered"]}


def topic_mastery(conn: sqlite3.Connection, topic_id: str) -> float:
    return mastery_map(conn).get(topic_id, 0.0)


def display_status(status: str, topic_id: str, mastered: set[str]) -> str:
    return "mastered" if status == "completed" and topic_id in mastered else status


def check_mastery(conn: sqlite3.Connection, topic_id: str) -> dict | None:
    """Award the second half of a topic's XP the first time it becomes mastered."""
    ev = mastery_evidence(conn, topic_id)
    if not ev["mastered"]:
        return None
    p = one(conn.execute("SELECT mastered_at FROM progress WHERE topic_id=?", (topic_id,)))
    if p["mastered_at"]:
        return None
    t = one(conn.execute("SELECT title, xp FROM topics WHERE id=?", (topic_id,)))
    conn.execute("UPDATE progress SET mastered_at=? WHERE topic_id=?", (iso(), topic_id))
    award = award_xp(conn, (t["xp"] or 60) // 2 + 25, f"mastered {t['title']} ⭐", topic_id)
    notify(conn, "achievement", f"⭐ Mastered: {t['title']}",
           "Proven unaided, after a delay. That star is lit for real.")
    return {"topic_id": topic_id, "award": award}


# --- Questions --------------------------------------------------------------------


def answer_display(q: dict) -> str:
    if q["type"] == "mc":
        return json.loads(q["choices"])[int(q["answer"])]
    return q["answer"] or ""


def public_question(conn: sqlite3.Connection, question_id: int) -> dict | None:
    q = one(conn.execute(
        "SELECT q.id, q.topic_id, q.type, q.prompt, q.choices, q.var, q.difficulty, q.verified, "
        "t.title AS topic_title FROM questions q JOIN topics t ON t.id=q.topic_id WHERE q.id=?",
        (question_id,)))
    if q and q["choices"]:
        q["choices"] = json.loads(q["choices"])
    return q


def _studied_topics(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT topic_id FROM progress WHERE status IN ('learning','completed')")]


def pick_questions(conn: sqlite3.Connection, topic_id: str | None = None, n: int = 5, mode: str = "topic",
                   auto_only: bool = False) -> list[dict]:
    """mode: topic | pretest (attempt-first, before the lesson) | mixed (interleaved look-alike
    types) | weak | mistakes | block (cumulative test over the current training block)."""
    base = "SELECT q.id, q.topic_id FROM questions q JOIN progress p ON p.topic_id=q.topic_id " \
           "WHERE q.active=1"
    if auto_only or mode in ("mixed", "block", "pretest"):
        base += " AND q.type IN ('mc','num','expr','antideriv')"
    if mode in ("topic", "pretest") and topic_id:
        cand = rows(conn.execute(base + " AND q.topic_id=?", (topic_id,)))
        random.shuffle(cand)
        if mode == "pretest":
            cand.sort(key=lambda c: conn.execute("SELECT difficulty FROM questions WHERE id=?",
                                                 (c["id"],)).fetchone()[0])
            n = min(n, 2)
    elif mode == "mistakes":
        cand = rows(conn.execute(
            base + " AND q.id IN (SELECT question_id FROM attempts a WHERE correct=0 AND NOT "
            "EXISTS (SELECT 1 FROM attempts b WHERE b.question_id=a.question_id AND b.correct=1 "
            "AND b.ts > a.ts))"))
        random.shuffle(cand)
    elif mode == "block":
        start = block_info(conn)["start"]
        cand = rows(conn.execute(base + " AND p.status='completed' AND p.completed_at >= ?",
                                 (start,)))
        if len(cand) < n:
            cand = rows(conn.execute(base + " AND p.status='completed'"))
        random.shuffle(cand)
        by_topic: dict[str, list] = {}
        for c in cand:
            by_topic.setdefault(c["topic_id"], []).append(c)
        cand = _round_robin(list(by_topic.values()))
    elif mode == "mixed":
        cand = _interleaved(conn, base, n)
    else:  # weak
        cand = rows(conn.execute(base + " AND p.status IN ('learning','completed')"))
        if not cand:
            cand = rows(conn.execute(base + " AND q.topic_id=?", (next_topic(conn)["id"],)))
        m = mastery_map(conn)
        cand.sort(key=lambda c: (m.get(c["topic_id"], 0) + random.random() * 0.15))
    return [public_question(conn, c["id"]) for c in cand[:n]]


def _round_robin(lists: list[list]) -> list:
    """Alternate between lists: a1 b1 c1 a2 b2 ..."""
    lists, out = [list(x) for x in lists if x], []
    while lists:
        for lst in lists:
            out.append(lst.pop())
        lists = [x for x in lists if x]
    return out


def _interleaved(conn: sqlite3.Connection, base: str, n: int) -> list[dict]:
    """Interleave look-alike problem types (u-sub vs parts, permutations vs combinations ...)
    from topics you've studied, then fill with a mastery-weighted mix."""
    studied = set(_studied_topics(conn))
    groups = [json.loads(r[0]) for r in conn.execute("SELECT topics FROM interleave_groups")]
    usable = [[t for t in g if t in studied] for g in groups]
    usable = [g for g in usable if len(g) >= 2]
    random.shuffle(usable)
    picked: list[dict] = []
    for g in usable:
        per = [rows(conn.execute(base + " AND q.topic_id=? ORDER BY RANDOM() LIMIT 2", (t,)))
               for t in g]
        picked += _round_robin(per)
        if len(picked) >= n:
            break
    seen = {c["id"] for c in picked}
    pool = rows(conn.execute(base + " AND p.status IN ('learning','completed')"))
    if not pool and not picked:
        nt = next_topic(conn)["id"]
        pool = rows(conn.execute(base + " AND q.topic_id=?", (nt,))) if nt else []
    m = mastery_map(conn)
    pool = [c for c in pool if c["id"] not in seen]
    pool.sort(key=lambda c: random.random() * (1.2 - m.get(c["topic_id"], 0)), reverse=True)
    out = picked[:n] + pool[: max(0, n - len(picked))]
    random.shuffle(out)
    return out


def _recent_attempts(conn: sqlite3.Connection, question_id: int, since: datetime) -> list[dict]:
    return rows(conn.execute("SELECT * FROM attempts WHERE question_id=? AND ts >= ? "
                             "ORDER BY id", (question_id, iso(since))))


def _hints_used(conn: sqlite3.Connection, question_id: int, since: datetime) -> list[dict]:
    return rows(conn.execute("SELECT * FROM hint_events WHERE question_id=? AND ts >= ? "
                             "ORDER BY id", (question_id, iso(since))))


def record_answer(conn: sqlite3.Connection, question_id: int, given: str, channel: str = "ui",
                  notification_id: int | None = None, card_id: int | None = None,
                  correct: bool | None = None, score: float | None = None,
                  confidence: int | None = None) -> dict:
    """Grade (or accept a verdict via `correct` for free-response) and record an attempt.

    Guardrail: the answer and worked solution are returned only when the attempt is correct.
    After a miss you can retry, climb the hint ladder (get_hint), or reveal the solution."""
    q = one(conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)))
    if not q:
        raise ValueError(f"no question {question_id}")
    if q["source"] == "diagnostic":
        raise ValueError("diagnostic questions are answered via the placement test")
    if correct is None:
        g = grading.check(q, given)
        if g.get("unparseable"):
            return {"question_id": question_id, "correct": None, "retry": True,
                    "feedback": g["feedback"]}
        correct = g["correct"]
        feedback = g["feedback"]
        if correct is None:  # free response: the model answer is shown AFTER an attempt
            return {"question_id": question_id, "correct": None, "needs_grading": True,
                    "model_answer": q["answer"], "explanation": q["explanation"]}
    else:
        feedback = ""
    correct = bool(correct)
    since = utcnow() - HINT_WINDOW
    prior = _recent_attempts(conn, question_id, since)
    hints = _hints_used(conn, question_id, since)
    revealed = any(h["level"] >= 4 for h in hints)
    aided = channel == "assistant"
    first_try = not prior
    if notification_id:
        n = one(conn.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)))
        if n and not n["answered"] and n["kind"] == "popquiz":
            conn.execute("UPDATE notifications SET answered=1, read=1 WHERE id=?",
                         (notification_id,))
            channel = "pop"
    conn.execute(
        "INSERT INTO attempts(question_id, topic_id, given, correct, score, channel, ts, aided, "
        "hints, revealed, confidence, first_try) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (question_id, q["topic_id"], given, int(correct),
         score if score is not None else float(correct), channel, iso(), int(aided),
         len(hints), int(revealed), confidence, int(first_try)))
    result = {"question_id": question_id, "correct": correct, "feedback": feedback,
              "first_try": first_try, "hints_used": len(hints)}
    if correct or revealed or channel == "pretest":
        result.update(answer=answer_display(q), explanation=q["explanation"])
    else:
        result.update(can_retry=True, next_hint_level=min(4, len(hints) + 1),
                      guidance="Not yet. Try again, or ask for a hint (get_hint). The solution "
                               "unlocks via reveal_solution.")
    if confidence == 1 and not correct:
        result["confident_miss"] = True
    # XP: quality over volume
    if channel == "pretest":
        xp, reason = 2, "attempt-first warm-up"
    elif correct:
        base = 10 * (q["difficulty"] or 1)
        quality = first_try and not hints and not aided
        xp, reason = (base, "correct, first try, unaided") if quality else \
            (max(2, base * 2 // 5), "correct (with help)" if hints or aided else "correct on retry")
        p = one(conn.execute("SELECT completed_at FROM progress WHERE topic_id=?",
                             (q["topic_id"],)))
        if quality and p["completed_at"] and \
                utcnow() - parse_iso(p["completed_at"]) >= timedelta(days=3):
            xp, reason = xp + 5, reason + " + delayed recall 🧠"
    else:
        xp, reason = 1, "attempt"
    result["award"] = award_xp(conn, xp, reason, q["topic_id"])
    # Spaced repetition (pretests don't touch cards: you haven't been taught it yet)
    if channel != "pretest":
        if card_id:
            result["schedule"] = apply_review(conn, card_id, GOOD if correct else AGAIN)
            if confidence == 1 and not correct:
                conn.execute("UPDATE cards SET priority=1 WHERE id=?", (card_id,))
        else:
            cid = _ensure_question_card(conn, question_id, activate=not correct)
            card = one(conn.execute("SELECT * FROM cards WHERE id=?", (cid,)))
            if not correct and first_try:
                result["schedule"] = apply_review(conn, cid, AGAIN)
                if confidence == 1:
                    conn.execute("UPDATE cards SET priority=1 WHERE id=?", (cid,))
            elif correct and card["active"] and card["last_review"] and card["due"] <= iso():
                result["schedule"] = apply_review(conn, cid, GOOD)
    if correct and not aided and not hints and not revealed and channel in UNAIDED_CHANNELS:
        result["mistakes_resolved"] = _auto_resolve_mistakes(conn, q["topic_id"], question_id)
    mark_studied(conn)
    _touch(conn, q["topic_id"])
    result["mastery"] = check_mastery(conn, q["topic_id"])
    result["achievements"] = check_achievements(conn)
    return result


def get_hint(conn: sqlite3.Connection, question_id: int, level: int | None = None, override: bool = False,
             channel: str = "ui") -> dict:
    """Hint ladder, unlocked only after an attempt on this question (last 12h):
    1 nudge · 2 method (solution with the final answer masked) · 3 similar worked example ·
    4 full solution (+ a fresh follow-up problem). Every hint is logged."""
    q = one(conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)))
    if not q:
        raise ValueError(f"no question {question_id}")
    since = utcnow() - HINT_WINDOW
    if not _recent_attempts(conn, question_id, since) and not override:
        raise ValueError("Attempt first: submit an answer before hints unlock. "
                         "(override=true records an override.)")
    used = _hints_used(conn, question_id, since)
    level = max(1, min(4, level or len(used) + 1))
    conn.execute("INSERT INTO hint_events(question_id, topic_id, level, override, channel, ts) "
                 "VALUES(?,?,?,?,?,?)", (question_id, q["topic_id"], level, int(override),
                                         channel, iso()))
    t = one(conn.execute("SELECT key_ideas FROM topics WHERE id=?", (q["topic_id"],)))
    ideas = json.loads(t["key_ideas"])
    out: dict = {"question_id": question_id, "level": level, "max_level": 4}
    if level == 1:
        out["hint"] = q["hint"] or (f"Key idea to use: {ideas[0]}" if ideas else
                                    "Re-read the question: what is it really asking for?")
    elif level == 2:
        ans = answer_display(q)
        method = (q["explanation"] or "").replace(ans, "▢") if ans else q["explanation"]
        out["hint"] = f"Method: {method}" if method else "Write the first step you'd take."
    elif level == 3:
        ex = one(conn.execute(
            "SELECT * FROM questions WHERE topic_id=? AND id != ? AND active=1 AND "
            "explanation != '' ORDER BY RANDOM() LIMIT 1", (q["topic_id"], question_id)))
        out["worked_example"] = ex and {"prompt": ex["prompt"], "answer": answer_display(ex),
                                        "solution": ex["explanation"]}
        if not ex:
            out["hint"] = "No sibling example yet -- ask the assistant for a parallel worked example."
    else:
        out.update(answer=answer_display(q), solution=q["explanation"], revealed=True)
        cid = _ensure_question_card(conn, question_id, activate=True)
        card = one(conn.execute("SELECT * FROM cards WHERE id=?", (cid,)))
        if not card["last_review"] or card["due"] > iso(utcnow() + timedelta(minutes=10)):
            apply_review(conn, cid, AGAIN)
        follow = one(conn.execute(
            "SELECT q.id FROM questions q LEFT JOIN attempts a ON a.question_id=q.id "
            "WHERE q.topic_id=? AND q.id != ? AND q.active=1 AND q.type != 'free' "
            "GROUP BY q.id ORDER BY MAX(a.ts) IS NOT NULL, MAX(a.ts) LIMIT 1",
            (q["topic_id"], question_id)))
        out["followup_question"] = follow and public_question(conn, follow["id"])
        out["self_explain"] = "In one sentence: why does this solution work?"
    return out


def save_self_explanation(conn: sqlite3.Connection, question_id: int, text: str) -> dict:
    a = one(conn.execute("SELECT id FROM attempts WHERE question_id=? ORDER BY id DESC LIMIT 1",
                         (question_id,)))
    if not a:
        raise ValueError("no attempt to attach this explanation to")
    conn.execute("UPDATE attempts SET note=? WHERE id=?", (text[:1000], a["id"]))
    q = one(conn.execute("SELECT topic_id FROM questions WHERE id=?", (question_id,)))
    return {"saved": True, "award": award_xp(conn, 3, "self-explanation", q["topic_id"])}


def add_question(conn: sqlite3.Connection, topic_id: str, qtype: str, prompt: str, answer: str,
                 explanation: str = "", choices: list[str] | None = None, var: str | None = None,
                 difficulty: int = 1, hint: str | None = None, tol: float | None = None,
                 verify: str | None = None) -> dict:
    """Add a question. Auto-graded num/expr/antideriv questions MUST pass CAS verification:
    `verify` is a sympy expression whose value is the answer (for antideriv: the integrand)."""
    if qtype not in ("mc", "num", "expr", "antideriv", "free"):
        raise ValueError("type must be mc, num, expr, antideriv or free")
    if qtype == "mc" and (not choices or not str(answer).isdigit()
                          or not 0 <= int(answer) < len(choices)):
        raise ValueError("mc needs choices and answer = valid 0-based index")
    _require_topic(conn, topic_id)
    verified = 1
    if qtype in ("num", "expr", "antideriv"):
        if not verify:
            raise ValueError("CAS verification required: pass `verify`, a sympy expression that "
                             "computes the answer, e.g. diff(x**2*sin(x), x) or "
                             "integrate(exp(-x), (x, 0, oo)); for antideriv pass the integrand.")
        v = grading.verify_question(qtype, answer, verify, var)
        if not v["verified"]:
            raise ValueError(f"CAS disagrees: verify evaluates to {v['cas_value']} but answer is "
                             f"{answer}. Fix the question before showing it.")
    elif qtype == "mc":
        verified = 0  # can't be machine-checked; flagged in the UI
    cur = conn.execute(
        "INSERT INTO questions(topic_id, type, prompt, choices, answer, var, tol, explanation, "
        "hint, difficulty, source, created_at, verified, verify) "
        "VALUES(?,?,?,?,?,?,?,?,?,?, 'assistant', ?,?,?)",
        (topic_id, qtype, prompt, json.dumps(choices) if choices else None, str(answer), var, tol,
         explanation, hint, difficulty, iso(), verified, verify))
    qid = cur.lastrowid
    started = conn.execute("SELECT status FROM progress WHERE topic_id=?",
                           (topic_id,)).fetchone()[0] == "completed"
    if qtype != "free":
        _ensure_question_card(conn, qid, activate=started)
    return {"question_id": qid, "topic_id": topic_id, "verified": bool(verified)}


def add_card(conn: sqlite3.Connection, topic_id: str, front: str, back: str, kind: str = "concept") -> dict:
    _require_topic(conn, topic_id)
    cur = conn.execute(
        "INSERT INTO cards(topic_id, front, back, active, due, created_at, kind) "
        "VALUES(?,?,?,0,?,?,?)", (topic_id, front, back, iso(), iso(), kind))
    _activate_card(conn, cur.lastrowid)
    return {"card_id": cur.lastrowid}


# --- Topics & progress ----------------------------------------------------------


def _require_topic(conn: sqlite3.Connection, topic_id: str) -> dict:
    t = one(conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)))
    if not t:
        raise ValueError(f"unknown topic '{topic_id}' -- call get_curriculum for valid ids")
    return t


def _touch(conn: sqlite3.Connection, topic_id: str) -> None:
    conn.execute("UPDATE progress SET last_activity=? WHERE topic_id=?", (iso(), topic_id))
    conn.execute("UPDATE progress SET status='learning', started_at=? "
                 "WHERE topic_id=? AND status='new'", (iso(), topic_id))


def start_topic(conn: sqlite3.Connection, topic_id: str) -> dict:
    t = _require_topic(conn, topic_id)
    p = one(conn.execute("SELECT * FROM progress WHERE topic_id=?", (topic_id,)))
    out: dict = {"topic_id": topic_id, "title": t["title"],
                 "attempt_first": "Start with 1-2 warm-up problems BEFORE the reading "
                                  "(get_quiz mode=pretest). Miss? Study the worked solution, "
                                  "then read."}
    if p["status"] == "new":
        conn.execute("UPDATE progress SET status='learning', started_at=?, last_activity=? "
                     "WHERE topic_id=?", (iso(), iso(), topic_id))
        out["award"] = award_xp(conn, 5, f"started {t['title']}", topic_id)
    out["achievements"] = check_achievements(conn)
    return out


def complete_topic(conn: sqlite3.Connection, topic_id: str) -> dict:
    """Provisional completion: half the topic XP now, unlock fun facts, cards enter FSRS as new
    cards. The other half comes when the topic is MASTERED (unaided, delayed evidence)."""
    t = _require_topic(conn, topic_id)
    p = one(conn.execute("SELECT * FROM progress WHERE topic_id=?", (topic_id,)))
    if p["status"] == "completed":
        return {"topic_id": topic_id, "already_completed": True}
    conn.execute("UPDATE progress SET status='completed', completed_at=?, last_activity=?, "
                 "started_at=COALESCE(started_at, ?) WHERE topic_id=?",
                 (iso(), iso(), iso(), topic_id))
    award = award_xp(conn, (t["xp"] or 60) // 2, f"completed {t['title']} (provisional)", topic_id)
    for r in conn.execute("SELECT id FROM cards WHERE topic_id=? AND active=0",
                          (topic_id,)).fetchall():
        _activate_card(conn, r[0])
    for r in conn.execute("SELECT id FROM questions WHERE topic_id=? AND active=1 AND "
                          "type != 'free'", (topic_id,)).fetchall():
        _ensure_question_card(conn, r[0], activate=True)
    conn.execute("UPDATE facts SET unlocked_at=? WHERE topic_id=? AND unlocked_at IS NULL",
                 (iso(), topic_id))
    facts = rows(conn.execute("SELECT * FROM facts WHERE topic_id=?", (topic_id,)))
    if facts:
        f = facts[0]
        icon = "🧩" if f["kind"] == "puzzle" else "✨"
        notify(conn, "fact", f"{icon} You just learned {t['title']}! {f['title']}",
               _strip_tex(f["body"]), link=f"{config.public_url()}/#/facts")
    mark_studied(conn)
    return {"topic_id": topic_id, "award": award, "unlocked_facts": facts,
            "provisional": True,
            "to_master": "Mastery needs: score ≥ 0.90, 3 different problems right unaided, and one "
                         "of them ≥3 days later (or in a mixed/review quiz).",
            "next_topic": next_topic(conn), "achievements": check_achievements(conn)}


def _strip_tex(s: str) -> str:
    return s.replace("$", "").replace("\\", "")


_UNIT_ORDER = "JOIN units u ON u.id=t.unit_id JOIN courses c ON c.id=u.course"


def next_topic(conn: sqlite3.Connection, course: str | None = None) -> dict:
    """In-progress topic first, else the first un-started required topic whose prereqs are
    done -- within `course` if given. Flags a review backlog (no new topics until cleared)."""
    where, args = ("AND u.course=?", [course]) if course else ("", [])
    backlog = due_count(conn) > setting(conn, "backlog_limit")
    learning = one(conn.execute(
        "SELECT t.id, t.title, t.unit_id, u.course FROM topics t JOIN progress p "
        f"ON p.topic_id=t.id {_UNIT_ORDER} WHERE p.status='learning' {where} "
        "ORDER BY p.last_activity DESC LIMIT 1", args))
    if learning:
        return {**learning, "why": "you're in the middle of this one", "backlog": backlog}
    done = {r[0] for r in conn.execute("SELECT topic_id FROM progress WHERE status='completed'")}
    pending = None
    for t in rows(conn.execute(
            "SELECT t.id, t.title, t.unit_id, t.prereqs, t.optional, u.course FROM topics t "
            f"{_UNIT_ORDER} WHERE 1=1 {where} ORDER BY c.ord, u.ord, t.ord", args)):
        if t["id"] in done or t["optional"]:
            continue
        if all(p in done for p in json.loads(t["prereqs"])):
            return {"id": t["id"], "title": t["title"], "unit_id": t["unit_id"],
                    "course": t["course"], "why": "next in the pathway", "backlog": backlog}
        pending = pending or t
    if pending:  # everything left is blocked on prereqs (e.g. optional ones skipped)
        return {"id": pending["id"], "title": pending["title"], "unit_id": pending["unit_id"],
                "course": pending["course"], "why": "next required topic (check its prereqs)",
                "backlog": backlog}
    return {"id": None, "title": "Everything complete — you are a legend", "why": "",
            "course": course, "backlog": backlog}


def get_topic(conn: sqlite3.Connection, topic_id: str) -> dict:
    """Topic lesson plan. Questions never include answers: solutions unlock per attempt."""
    t = _require_topic(conn, topic_id)
    for k in ("key_ideas", "readings", "videos", "prereqs"):
        t[k] = json.loads(t[k])
    p = one(conn.execute("SELECT * FROM progress WHERE topic_id=?", (topic_id,)))
    m = mastery_map(conn)
    t["mastery"] = m.get(topic_id, 0.0)
    t["mastery_evidence"] = mastery_evidence(conn, topic_id, t["mastery"])
    t["status"] = "mastered" if t["mastery_evidence"]["mastered"] else p["status"]
    t["progress"] = p
    t["unit"] = one(conn.execute("SELECT id, title, constellation, color, course FROM units "
                                 "WHERE id=?", (t["unit_id"],)))
    t["questions"] = [public_question(conn, q[0]) for q in conn.execute(
        "SELECT id FROM questions WHERE topic_id=? AND active=1 ORDER BY id", (topic_id,))]
    t["stats"] = one(conn.execute(
        "SELECT COUNT(*) AS attempts, COALESCE(SUM(correct),0) AS correct, "
        "COALESCE(SUM(aided=0 AND hints=0 AND correct=1),0) AS unaided_correct FROM attempts "
        "WHERE topic_id=? AND channel NOT LIKE 'diag:%'", (topic_id,)))
    t["hints_used"] = conn.execute("SELECT COUNT(*) FROM hint_events WHERE topic_id=?",
                                   (topic_id,)).fetchone()[0]
    t["grades"] = rows(conn.execute("SELECT score, kind, notes, ts FROM grades WHERE topic_id=? "
                                    "ORDER BY ts DESC LIMIT 10", (topic_id,)))
    t["mistakes"] = rows(conn.execute("SELECT * FROM mistakes WHERE topic_id=? ORDER BY ts DESC",
                                      (topic_id,)))
    t["facts"] = rows(conn.execute(
        "SELECT id, kind, title, body, answer FROM facts WHERE topic_id=? AND unlocked_at IS NOT "
        "NULL", (topic_id,)))
    t["cards_total"] = conn.execute("SELECT COUNT(*) FROM cards WHERE topic_id=?",
                                    (topic_id,)).fetchone()[0]
    t["lessons"] = lessons_for_topic(conn, topic_id)
    return t


def courses(conn: sqlite3.Connection) -> list[dict]:
    """Courses with progress summary and next topic each."""
    out = rows(conn.execute("SELECT id, title, subtitle, color, blurb FROM courses ORDER BY ord"))
    for c in out:
        c.update(one(conn.execute(
            "SELECT COUNT(*) AS topics, COALESCE(SUM(p.status='completed'),0) AS completed, "
            "COALESCE(SUM(p.mastered_at IS NOT NULL),0) AS mastered_ever, "
            "COUNT(DISTINCT u.id) AS units FROM topics t JOIN units u ON u.id=t.unit_id "
            "JOIN progress p ON p.topic_id=t.id WHERE u.course=?", (c["id"],))))
        c["next_topic"] = next_topic(conn, c["id"])
    return out


def curriculum(conn: sqlite3.Connection, course: str | None = None) -> list[dict]:
    m = mastery_map(conn)
    mastered = mastered_set(conn, m)
    q, a = "SELECT u.* FROM units u JOIN courses c ON c.id=u.course", []
    if course:
        q, a = q + " WHERE u.course=?", [course]
    units = rows(conn.execute(q + " ORDER BY c.ord, u.ord", a))
    topics = rows(conn.execute(
        "SELECT t.id, t.unit_id, t.ord, t.title, t.prereqs, t.optional, t.xp, t.readings, "
        "p.status FROM topics t JOIN progress p ON p.topic_id=t.id ORDER BY t.ord"))
    for u in units:
        u["topics"] = []
        for t in topics:
            if t["unit_id"] == u["id"]:
                t["mastery"] = m.get(t["id"], 0)
                t["status"] = display_status(t["status"], t["id"], mastered)
                t["prereqs"] = json.loads(t["prereqs"])
                t["readings"] = json.loads(t["readings"])
                u["topics"].append(t)
        core = [t for t in u["topics"] if not t["optional"]] or u["topics"]
        u["mastery"] = round(sum(t["mastery"] for t in core) / len(core), 3) if core else 0
        u["completed"] = sum(t["status"] in ("completed", "mastered") for t in u["topics"])
        u["mastered"] = sum(t["status"] == "mastered" for t in u["topics"])
    return units


# --- Grades, mistakes, journal -----------------------------------------------------


def record_grade(conn: sqlite3.Connection, topic_id: str, score: float, kind: str = "quiz", notes: str = "") -> dict:
    _require_topic(conn, topic_id)
    score = max(0.0, min(100.0, float(score)))
    conn.execute("INSERT INTO grades(topic_id, score, kind, notes, ts) VALUES(?,?,?,?,?)",
                 (topic_id, score, kind, notes, iso()))
    award = award_xp(conn, int(5 + score / 10), f"{kind} graded {score:.0f}%", topic_id)
    _touch(conn, topic_id)
    return {"topic_id": topic_id, "score": score, "letter": letter(score),
            "mastery": topic_mastery(conn, topic_id), "award": award,
            "mastered": check_mastery(conn, topic_id), "achievements": check_achievements(conn)}


def letter(score: float) -> str:
    for cut, g in ((97, "A+"), (93, "A"), (90, "A-"), (87, "B+"), (83, "B"), (80, "B-"),
                   (77, "C+"), (73, "C"), (70, "C-"), (60, "D")):
        if score >= cut:
            return g
    return "F"


def log_mistake(conn: sqlite3.Connection, topic_id: str, what: str, misconception: str = "", fix: str = "",
                question_id: int | None = None, confident: bool = False) -> dict:
    """Log a mistake. It becomes a spaced 'spot the error' card; confident misses get priority.
    It resolves only when you later get a NEW problem from the topic right, unaided."""
    _require_topic(conn, topic_id)
    front = f"⚠️ Spot the trap: {what}" + (f"\n\nWhat's the right idea?" if misconception else "")
    back = (f"**Misconception:** {misconception}\n\n" if misconception else "") + \
        (f"**Fix:** {fix}" if fix else "Explain the correct reasoning out loud, then check.")
    card = add_card(conn, topic_id, front, back, kind="mistake")["card_id"]
    if confident:
        conn.execute("UPDATE cards SET priority=1 WHERE id=?", (card,))
    cur = conn.execute(
        "INSERT INTO mistakes(topic_id, question_id, what, misconception, fix, ts, card_id, "
        "confident) VALUES(?,?,?,?,?,?,?,?)",
        (topic_id, question_id, what, misconception, fix, iso(), card, int(confident)))
    if question_id:
        cid = _ensure_question_card(conn, question_id, activate=True)
        if confident:
            conn.execute("UPDATE cards SET priority=1 WHERE id=?", (cid,))
    award = award_xp(conn, 5 if misconception else 2, "logged a mistake (reflection!)", topic_id)
    return {"mistake_id": cur.lastrowid, "card_id": card, "award": award,
            "achievements": check_achievements(conn)}


def _resolution_evidence(conn: sqlite3.Connection, m: dict) -> dict | None:
    return one(conn.execute(
        "SELECT * FROM attempts WHERE topic_id=? AND correct=1 AND aided=0 AND hints=0 AND "
        "revealed=0 AND ts >= ? AND (question_id != ? OR ? IS NULL) AND channel NOT LIKE "
        "'diag:%' ORDER BY ts LIMIT 1",
        (m["topic_id"], iso(parse_iso(m["ts"]) + timedelta(days=1)), m["question_id"],
         m["question_id"])))


def _resolve(conn: sqlite3.Connection, m: dict, note: str = "", override: bool = False) -> dict:
    conn.execute("UPDATE mistakes SET resolved=1, resolved_at=?, override=?, "
                 "fix=COALESCE(NULLIF(?,''), fix) WHERE id=?",
                 (iso(), int(override), note, m["id"]))
    if m.get("card_id"):
        conn.execute("UPDATE cards SET active=0 WHERE id=?", (m["card_id"],))
    return award_xp(conn, 5 if override else 15, "conquered a mistake", m["topic_id"])


def _auto_resolve_mistakes(conn: sqlite3.Connection, topic_id: str, question_id: int) -> list[int]:
    done = []
    for m in rows(conn.execute("SELECT * FROM mistakes WHERE topic_id=? AND resolved=0 "
                               "AND ts <= ? AND (question_id IS NULL OR question_id != ?)",
                               (topic_id, iso(utcnow() - timedelta(days=1)), question_id)))[:3]:
        _resolve(conn, m)
        done.append(m["id"])
    return done


def resolve_mistake(conn: sqlite3.Connection, mistake_id: int, note: str = "", override: bool = False) -> dict:
    """Resolve only with evidence: a correct, unaided answer to a different problem in the topic
    at least a day after the mistake. override=True forces it (recorded, less XP)."""
    m = one(conn.execute("SELECT * FROM mistakes WHERE id=?", (mistake_id,)))
    if not m:
        raise ValueError(f"no mistake {mistake_id}")
    if m["resolved"]:
        return {"mistake_id": mistake_id, "already_resolved": True}
    if not override and not _resolution_evidence(conn, m):
        raise ValueError("Not yet: this resolves when you answer a NEW problem from this topic "
                         "correctly, without hints, at least a day later (it happens "
                         "automatically). Pass override=true to force it.")
    award = _resolve(conn, m, note, override)
    return {"mistake_id": mistake_id, "award": award, "override": override,
            "achievements": check_achievements(conn)}


def add_journal(conn: sqlite3.Connection, summary: str, topics: list[str] | None = None, next_steps: str = "",
                mood: str = "") -> dict:
    cur = conn.execute("INSERT INTO journal(ts, summary, topics, next_steps, mood) "
                       "VALUES(?,?,?,?,?)",
                       (iso(), summary, json.dumps(topics or []), next_steps, mood))
    award = award_xp(conn, 5, "tutoring session")
    return {"journal_id": cur.lastrowid, "award": award, "achievements": check_achievements(conn)}


# --- Training blocks (6 weeks, ending in a cumulative test) ------------------------

BLOCK_WEEKS = 6


def block_info(conn: sqlite3.Connection) -> dict:
    start = get_setting(conn, "block_start")
    if not start:
        first = conn.execute("SELECT MIN(local_date) FROM study_days WHERE studied=1"
                             ).fetchone()[0]
        start = first or local_today().isoformat()
        set_setting(conn, "block_start", start)
    start_d = date.fromisoformat(start)
    day = (local_today() - start_d).days
    history = get_setting(conn, "blocks", []) or []
    return {"number": len(history) + 1, "start": start, "day": day,
            "week": min(BLOCK_WEEKS, day // 7 + 1), "weeks": BLOCK_WEEKS,
            "test_ready": day >= BLOCK_WEEKS * 7 - 2, "history": history[-6:]}


def complete_block(conn: sqlite3.Connection, correct: int, total: int) -> dict:
    b = block_info(conn)
    score = round(100 * correct / max(1, total), 1)
    hist = get_setting(conn, "blocks", []) or []
    hist.append({"number": b["number"], "start": b["start"], "end": local_today().isoformat(),
                 "score": score, "correct": correct, "total": total})
    set_setting(conn, "blocks", hist)
    set_setting(conn, "block_start", local_today().isoformat())
    award = award_xp(conn, 50 + int(score / 2), f"training block {b['number']} test: {score:.0f}%")
    return {"block": b["number"], "score": score, "award": award,
            "achievements": check_achievements(conn)}


# --- Measurement: the three numbers that matter -----------------------------------


def learning_health(conn: sqlite3.Connection, days: int = 30) -> dict:
    since = iso(utcnow() - timedelta(days=days))
    delayed = one(conn.execute(
        f"SELECT COUNT(*) AS n, AVG(a.correct) AS acc FROM attempts a JOIN progress p "
        f"ON p.topic_id=a.topic_id WHERE a.ts >= ? AND a.aided=0 AND a.hints=0 AND "
        f"(a.channel IN ({','.join('?' * len(DELAYED_CHANNELS))}) OR (p.completed_at IS NOT NULL "
        f"AND julianday(a.ts) >= julianday(p.completed_at) + 3))", (since, *DELAYED_CHANNELS)))
    hint_n = conn.execute("SELECT COUNT(*) FROM hint_events WHERE ts >= ?", (since,)).fetchone()[0]
    problems = conn.execute("SELECT COUNT(DISTINCT question_id) FROM attempts WHERE ts >= ? AND "
                            "channel NOT LIKE 'diag:%'", (since,)).fetchone()[0]
    cal = one(conn.execute("SELECT COUNT(*) AS n, AVG(confidence) AS conf, AVG(correct) AS acc "
                           "FROM attempts WHERE ts >= ? AND confidence IS NOT NULL", (since,)))
    return {
        "window_days": days,
        "unaided_delayed_accuracy": {"n": delayed["n"], "value": delayed["acc"]},
        "hints_per_problem": {"hints": hint_n, "problems": problems,
                              "value": round(hint_n / problems, 2) if problems else None},
        "calibration_gap": {"n": cal["n"],
                            "value": round(cal["conf"] - cal["acc"], 3) if cal["n"] else None,
                            "note": "confident-rate minus accuracy; >0 = overconfident"},
    }


def weekly_summary(conn: sqlite3.Connection) -> dict:
    c = consistency(conn)
    since = c["week"]["start"]
    xp = conn.execute("SELECT COALESCE(SUM(amount),0) FROM xp_events WHERE local_date >= ?",
                      (since,)).fetchone()[0]
    done = rows(conn.execute("SELECT t.title FROM progress p JOIN topics t ON t.id=p.topic_id "
                             "WHERE p.completed_at >= ?", (since,)))
    mastered = rows(conn.execute("SELECT t.title FROM progress p JOIN topics t "
                                 "ON t.id=p.topic_id WHERE p.mastered_at >= ?", (since,)))
    h = learning_health(conn, 7)
    acc = h["unaided_delayed_accuracy"]["value"]
    text = (f"📐 Calculus Quest, week of {since}: studied {c['week']['days']}/"
            f"{c['week']['target']} days · {len(done)} lessons done"
            + (f" ({', '.join(d['title'] for d in done[:3])})" if done else "")
            + (f" · {len(mastered)} mastered ⭐" if mastered else "")
            + (f" · unaided recall {acc:.0%}" if acc is not None else "")
            + f" · never-miss-twice chain {c['chain']} days.")
    return {"text": text, "xp": xp, "consistency": c, "completed": done, "mastered": mastered}


# --- Achievements -----------------------------------------------------------------

ACHIEVEMENTS: dict[str, tuple[str, str, str, int]] = {
    # key: (name, description, icon, bonus xp)
    "first_steps": ("First Steps", "Start your first topic", "👣", 10),
    "first_light": ("First Light", "Complete your first topic", "🌟", 25),
    "first_star": ("True Star", "Master your first topic (unaided, after a delay)", "⭐", 50),
    "chain_7": ("Never Miss Twice", "7-day never-miss-twice chain", "🔗", 30),
    "chain_30": ("Iron Chain", "30-day never-miss-twice chain", "⛓️", 100),
    "chain_100": ("Centurion", "100-day never-miss-twice chain", "🏛️", 300),
    "weeks_4": ("Consistency", "Hit your weekly target 4 times", "📅", 60),
    "days_50": ("Fifty Sessions", "Study on 50 different days", "🗓️", 80),
    "hot_5": ("Hot Hand", "5 unaided first-try answers in a row", "🎯", 20),
    "century": ("Century", "100 unaided correct answers", "💯", 100),
    "marathon_mind": ("Marathon Mind", "262 answers attempted (26.2 × 10)", "🏃", 150),
    "recall_50": ("Long Memory", "50 delayed-recall successes (≥3 days later)", "🧠", 80),
    "reviews_500": ("Elephant", "500 reviews", "🐘", 150),
    "reflective": ("Metacognition", "Log 5 mistakes with the misconception behind them", "🪞", 40),
    "bug_squasher": ("Bug Squasher", "Conquer 10 mistakes (proven on new problems)", "🐛", 80),
    "calibrated": ("Calibrated", "50 confidence-rated answers with |overconfidence| < 10%", "⚖️",
                   60),
    "self_reliant": ("Self-Reliant", "20 problems in a row solved without hints", "🧗", 40),
    "block_1": ("Block Complete", "Finish a 6-week training block test", "🏁", 0),
    "star_collector": ("Star Collector", "Master 10 topics", "✴️", 150),
    "polymath": ("Polymath", "Study topics in 5 different units", "🧭", 50),
    "tutor_pet": ("Tutor's Pet", "Log 5 tutoring sessions with your assistant", "🍎", 20),
    "know_thyself": ("Know Thyself", "Finish the placement test", "🧭", 50),
    "level_5": ("Tangent Tamer", "Reach level 5", "🎖️", 0),
    "level_10": ("Integral Artisan", "Reach level 10", "🏅", 0),
    "level_20": ("Newton–Leibniz Fellow", "Reach level 20", "👑", 0),
}


def achievement_catalog(conn: sqlite3.Connection) -> list[dict]:
    unlocked = {r[0]: r[1] for r in conn.execute("SELECT key, unlocked_at FROM achievements")}
    cat = [{"key": k, "name": n, "desc": d, "icon": i, "xp": x, "unlocked_at": unlocked.get(k)}
           for k, (n, d, i, x) in ACHIEVEMENTS.items()]
    for u in rows(conn.execute("SELECT u.id, u.constellation, u.title FROM units u JOIN courses c "
                               "ON c.id=u.course ORDER BY c.ord, u.ord")):
        k = f"constellation_{u['id']}"
        cat.append({"key": k, "name": f"Constellation: {u['constellation']}",
                    "desc": f"Complete every core topic in {u['title']}", "icon": "🌌",
                    "xp": 100, "unlocked_at": unlocked.get(k)})
    # older achievements earned before the redesign stay visible
    known = {a["key"] for a in cat}
    for k, ts in unlocked.items():
        if k not in known:
            cat.append({"key": k, "name": k.replace("_", " ").title(), "desc": "(retired)",
                        "icon": "🏷️", "xp": 0, "unlocked_at": ts})
    return cat


def check_achievements(conn: sqlite3.Connection) -> list[dict]:
    have = {r[0] for r in conn.execute("SELECT key FROM achievements")}
    c = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    earned: list[str] = []

    def test(key: str, cond) -> None:
        if key not in have and cond():
            earned.append(key)

    unaided = "aided=0 AND hints=0 AND revealed=0 AND channel NOT LIKE 'diag:%'"
    test("first_steps", lambda: c("SELECT COUNT(*) FROM progress WHERE status!='new'") > 0)
    test("first_light", lambda: c("SELECT COUNT(*) FROM progress WHERE status='completed'") > 0)
    test("first_star", lambda: c("SELECT COUNT(*) FROM progress WHERE mastered_at IS NOT NULL")
         > 0)
    test("star_collector", lambda: c("SELECT COUNT(*) FROM progress WHERE mastered_at IS NOT "
                                     "NULL") >= 10)
    cons = consistency(conn)
    for n in (7, 30, 100):
        test(f"chain_{n}", lambda n=n: cons["chain"] >= n)
    test("weeks_4", lambda: (get_setting(conn, "weeks_hit", 0) or 0) >= 4)
    test("days_50", lambda: cons["lifetime_days"] >= 50)
    recent = [r[0] for r in conn.execute(
        f"SELECT correct AND first_try FROM attempts WHERE {unaided} AND channel != 'pretest' "
        "ORDER BY id DESC LIMIT 5")]
    test("hot_5", lambda: len(recent) == 5 and all(recent))
    test("century", lambda: c(f"SELECT COUNT(*) FROM attempts WHERE correct=1 AND {unaided}")
         >= 100)
    test("marathon_mind", lambda: c("SELECT COUNT(*) FROM attempts WHERE channel NOT LIKE "
                                    "'diag:%'") >= 262)
    test("recall_50", lambda: c("SELECT COUNT(*) FROM xp_events WHERE reason LIKE "
                                "'%delayed recall%'") >= 50)
    test("reviews_500", lambda: c("SELECT COUNT(*) FROM reviews") >= 500)
    test("reflective", lambda: c("SELECT COUNT(*) FROM mistakes WHERE misconception != ''") >= 5)
    test("bug_squasher", lambda: c("SELECT COUNT(*) FROM mistakes WHERE resolved=1 AND "
                                   "override=0") >= 10)
    cal = learning_health(conn, 90)["calibration_gap"]
    test("calibrated", lambda: cal["n"] >= 50 and cal["value"] is not None
         and abs(cal["value"]) < 0.10)
    last20 = [r[0] for r in conn.execute(
        "SELECT hints FROM attempts WHERE channel NOT LIKE 'diag:%' ORDER BY id DESC LIMIT 20")]
    test("self_reliant", lambda: len(last20) == 20 and not any(last20))
    test("block_1", lambda: len(get_setting(conn, "blocks", []) or []) >= 1)
    test("polymath", lambda: c("SELECT COUNT(DISTINCT t.unit_id) FROM topics t JOIN progress p "
                               "ON p.topic_id=t.id WHERE p.status!='new'") >= 5)
    test("tutor_pet", lambda: c("SELECT COUNT(*) FROM journal") >= 5)
    test("know_thyself", lambda: c("SELECT COUNT(*) FROM diag_runs WHERE finished_at IS NOT NULL")
         > 0)
    lvl = level_info(total_xp(conn))["level"]
    for n in (5, 10, 20):
        test(f"level_{n}", lambda n=n: lvl >= n)
    for u in conn.execute("SELECT id FROM units").fetchall():
        k = f"constellation_{u[0]}"
        test(k, lambda uid=u[0]: c(
            "SELECT COUNT(*) FROM topics t JOIN progress p ON p.topic_id=t.id WHERE t.unit_id=? "
            "AND t.optional=0 AND p.status!='completed'", uid) == 0)

    cat = {a["key"]: a for a in achievement_catalog(conn)}
    out = []
    for k in earned:
        conn.execute("INSERT OR IGNORE INTO achievements(key, unlocked_at) VALUES(?,?)", (k, iso()))
        a = cat[k]
        out.append(a)
        if a["xp"]:
            conn.execute("INSERT INTO xp_events(amount, reason, ts, local_date) VALUES(?,?,?,?)",
                         (a["xp"], f"achievement: {a['name']}", iso(), local_today().isoformat()))
        notify(conn, "achievement", f"{a['icon']} Achievement unlocked: {a['name']}",
               f"{a['desc']} (+{a['xp']} XP)")
    return out


# --- Dashboard ----------------------------------------------------------------------


def daily_fact(conn: sqlite3.Connection) -> dict | None:
    """Fact/puzzle of the day: deterministic per day, drawn from unlocked ones (or teasers)."""
    pool = rows(conn.execute("SELECT id, topic_id, kind, title, body, answer FROM facts "
                             "WHERE unlocked_at IS NOT NULL ORDER BY id"))
    if not pool:
        pool = rows(conn.execute("SELECT f.id, f.topic_id, f.kind, f.title, f.body, f.answer "
                                 "FROM facts f JOIN topics t ON t.id=f.topic_id "
                                 f"{_UNIT_ORDER} ORDER BY c.ord, u.ord, t.ord LIMIT 3"))
    if not pool:
        return None
    return pool[local_today().toordinal() % len(pool)]


def dashboard(conn: sqlite3.Connection) -> dict:
    from . import wellness  # local: wellness imports engine helpers

    xp = total_xp(conn)
    today = local_today().isoformat()
    week_ago = (local_today() - timedelta(days=6)).isoformat()
    due = due_count(conn)
    return {
        "level": level_info(xp),
        "consistency": consistency(conn),
        "streak": streak(conn),
        "today_xp": conn.execute("SELECT COALESCE(SUM(amount),0) FROM xp_events WHERE "
                                 "local_date=?", (today,)).fetchone()[0],
        "due_reviews": due,
        "new_cards_ready": min(new_due_count(conn), max(0, setting(conn, "new_cards_per_day")
                                                         - _reviews_today(conn)[1])),
        "backlog": due > setting(conn, "backlog_limit"),
        "next_topic": next_topic(conn),
        "courses": courses(conn),
        "topics_completed": conn.execute("SELECT COUNT(*) FROM progress WHERE "
                                         "status='completed'").fetchone()[0],
        "topics_mastered": conn.execute("SELECT COUNT(*) FROM progress WHERE "
                                        "mastered_at IS NOT NULL").fetchone()[0],
        "topics_total": conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0],
        "open_mistakes": conn.execute("SELECT COUNT(*) FROM mistakes WHERE resolved=0"
                                      ).fetchone()[0],
        "learning_health": learning_health(conn),
        "block": block_info(conn),
        "wellness": wellness.today(conn),
        "study_days": rows(conn.execute(
            "SELECT local_date AS day, studied, protected, reason FROM study_days "
            "WHERE local_date >= ? ORDER BY local_date",
            ((local_today() - timedelta(days=89)).isoformat(),))),
        "xp_by_day": rows(conn.execute(
            "SELECT local_date AS day, SUM(amount) AS xp FROM xp_events WHERE local_date >= ? "
            "GROUP BY local_date ORDER BY local_date",
            ((local_today() - timedelta(days=89)).isoformat(),))),
        "week_xp": conn.execute("SELECT COALESCE(SUM(amount),0) FROM xp_events WHERE "
                                "local_date >= ?", (week_ago,)).fetchone()[0],
        "recent_xp": rows(conn.execute("SELECT amount, reason, ts FROM xp_events "
                                       "ORDER BY id DESC LIMIT 8")),
        "fact_of_the_day": daily_fact(conn),
        "unread_notifications": conn.execute("SELECT COUNT(*) FROM notifications WHERE read=0"
                                             ).fetchone()[0],
        "last_session": one(conn.execute("SELECT * FROM journal ORDER BY id DESC LIMIT 1")),
        "diagnostic": {
            "taken": conn.execute("SELECT COUNT(*) FROM diag_runs WHERE finished_at IS NOT NULL"
                                  ).fetchone()[0] > 0,
            "in_progress": diag_current_run(conn) is not None,
        },
    }


# --- Placement diagnostic (a pretest with feedback) -----------------------------------

DIAG_STRONG, DIAG_REVIEW = 0.85, 0.6


def _diag_level(score: float) -> str:
    return "strong" if score >= DIAG_STRONG else "review" if score >= DIAG_REVIEW else "learn"


def diag_sections(conn: sqlite3.Connection) -> list[dict]:
    secs = rows(conn.execute("SELECT * FROM diag_sections ORDER BY ord"))
    for s in secs:
        s["topics"] = json.loads(s["topics"])
        s["question_ids"] = [r[0] for r in conn.execute(
            "SELECT id FROM questions WHERE key LIKE ? ORDER BY id", (f"diag:{s['id']}:%",))]
    return secs


def diag_current_run(conn: sqlite3.Connection) -> dict | None:
    return one(conn.execute("SELECT * FROM diag_runs WHERE finished_at IS NULL "
                            "ORDER BY id DESC LIMIT 1"))


def diag_start(conn: sqlite3.Connection) -> dict:
    run = diag_current_run(conn)
    if not run:
        cur = conn.execute("INSERT INTO diag_runs(started_at) VALUES(?)", (iso(),))
        run = one(conn.execute("SELECT * FROM diag_runs WHERE id=?", (cur.lastrowid,)))
    return diag_state(conn, run["id"])


def diag_state(conn: sqlite3.Connection, run_id: int | None = None, include_questions: bool = True) -> dict:
    """Sections + questions (no answers) + which are already answered in this run."""
    run = one(conn.execute("SELECT * FROM diag_runs WHERE id=?", (run_id,))) if run_id else \
        diag_current_run(conn)
    answered = {}
    if run:
        for r in conn.execute("SELECT question_id, correct FROM attempts WHERE channel=?",
                              (f"diag:{run['id']}",)):
            answered[r[0]] = bool(r[1])
    secs = diag_sections(conn)
    total = sum(len(s["question_ids"]) for s in secs)
    if include_questions:
        for s in secs:
            s["questions"] = [public_question(conn, q) | {"answered": q in answered}
                              for q in s["question_ids"]]
    last = one(conn.execute("SELECT * FROM diag_runs WHERE finished_at IS NOT NULL "
                            "ORDER BY id DESC LIMIT 1"))
    if last:
        last["results"] = json.loads(last["results"])
    return {"meta": get_setting(conn, "diagnostic_meta", {}),
            "run": run and {**run, "answered": len(answered), "total": total},
            "sections": secs, "last_result": last}


def diag_answer(conn: sqlite3.Connection, run_id: int, question_id: int, answer: str = "",
                dont_know: bool = False) -> dict:
    """Grade a placement answer and show the worked solution (pretest-with-feedback: seeing
    the answer right after trying is what makes pretesting help). No XP, no review cards."""
    run = one(conn.execute("SELECT * FROM diag_runs WHERE id=?", (run_id,)))
    if not run or run["finished_at"]:
        raise ValueError("no open diagnostic run with that id -- call diag_start")
    q = one(conn.execute("SELECT * FROM questions WHERE id=? AND source='diagnostic'",
                         (question_id,)))
    if not q:
        raise ValueError(f"{question_id} is not a diagnostic question")
    channel = f"diag:{run_id}"
    prior = one(conn.execute("SELECT correct FROM attempts WHERE question_id=? AND channel=?",
                             (question_id, channel)))
    base = {"question_id": question_id, "answer": answer_display(q),
            "explanation": q["explanation"]}
    if prior:
        return {**base, "correct": bool(prior["correct"]), "already_answered": True}
    if dont_know:
        correct, feedback = False, "skipped"
    else:
        g = grading.check(q, answer)
        if g.get("unparseable"):
            return {"question_id": question_id, "correct": None, "retry": True,
                    "feedback": g["feedback"]}
        correct, feedback = bool(g["correct"]), g["feedback"]
    conn.execute("INSERT INTO attempts(question_id, topic_id, given, correct, score, channel, ts) "
                 "VALUES(?,?,?,?,?,?,?)", (question_id, q["topic_id"],
                                           "(don't know)" if dont_know else answer, int(correct),
                                           float(correct), channel, iso()))
    mark_studied(conn)
    return {**base, "correct": correct, "feedback": feedback, "dont_know": dont_know}


def diag_finish(conn: sqlite3.Connection, run_id: int) -> dict:
    run = one(conn.execute("SELECT * FROM diag_runs WHERE id=?", (run_id,)))
    if not run:
        raise ValueError("unknown diagnostic run")
    if run["finished_at"]:
        return {"run_id": run_id, "results": json.loads(run["results"]), "already_finished": True}
    answered = {r[0]: r[1] for r in conn.execute(
        "SELECT question_id, correct FROM attempts WHERE channel=?", (f"diag:{run_id}",))}
    titles = dict(conn.execute("SELECT id, title FROM topics").fetchall())
    results = []
    for s in diag_sections(conn):
        qs = s["question_ids"]
        got = [answered[q] for q in qs if q in answered]
        score = sum(got) / len(qs) if qs else 0.0  # unanswered count as not known
        results.append({"id": s["id"], "title": s["title"], "correct": sum(got),
                        "answered": len(got), "total": len(qs), "score": round(score, 3),
                        "level": _diag_level(score),
                        "topics": [{"id": t, "title": titles.get(t, t)} for t in s["topics"]]})
    conn.execute("UPDATE diag_runs SET finished_at=?, results=? WHERE id=?",
                 (iso(), json.dumps(results), run_id))
    award = award_xp(conn, 50, "finished the placement test")
    overall = sum(r["correct"] for r in results) / max(1, sum(r["total"] for r in results))
    return {"run_id": run_id, "results": results, "overall": round(overall, 3), "award": award,
            "achievements": check_achievements(conn),
            "start_with": next((r for r in results if r["level"] != "strong"), None)}


def diag_apply(conn: sqlite3.Connection, run_id: int, section_ids: list[str] | None = None) -> dict:
    """Place out of the topics in the chosen sections (default: every 'strong' section):
    mark them completed (provisional), unlock their facts, and queue their cards for a
    confirmation review in two days."""
    run = one(conn.execute("SELECT * FROM diag_runs WHERE id=?", (run_id,)))
    if not run or not run["finished_at"]:
        raise ValueError("finish the diagnostic before applying placement")
    results = {r["id"]: r for r in json.loads(run["results"])}
    chosen = section_ids if section_ids is not None else \
        [k for k, r in results.items() if r["level"] == "strong"]
    placed = []
    due = utcnow() + timedelta(days=2)
    for sid in chosen:
        if sid not in results:
            raise ValueError(f"unknown section {sid}")
        for t in results[sid]["topics"]:
            st = conn.execute("SELECT status FROM progress WHERE topic_id=?", (t["id"],)).fetchone()
            if not st or st[0] == "completed":
                continue
            conn.execute("UPDATE progress SET status='completed', completed_at=?, last_activity=?, "
                         "started_at=COALESCE(started_at, ?) WHERE topic_id=?",
                         (iso(), iso(), iso(), t["id"]))
            for r in conn.execute("SELECT id FROM cards WHERE topic_id=? AND active=0",
                                  (t["id"],)).fetchall():
                _activate_card(conn, r[0], due)
            for r in conn.execute("SELECT id FROM questions WHERE topic_id=? AND active=1 AND "
                                  "type != 'free'", (t["id"],)).fetchall():
                cid = _ensure_question_card(conn, r[0], activate=False)
                _activate_card(conn, cid, due)
            conn.execute("UPDATE facts SET unlocked_at=? WHERE topic_id=? AND unlocked_at IS NULL",
                         (iso(), t["id"]))
            placed.append(t)
    conn.execute("UPDATE diag_runs SET applied=1 WHERE id=?", (run_id,))
    award = award_xp(conn, 10 * len(placed), f"placed out of {len(placed)} topics") \
        if placed else None
    return {"placed": placed, "award": award, "next_topic": next_topic(conn, "core"),
            "achievements": check_achievements(conn)}


# --- Interactive lessons (explorables) -------------------------------------------------
# Guided practice: they earn a little XP and count as a study day, but never as mastery
# evidence (help was built in, like hints).


def lessons_for_topic(conn: sqlite3.Connection, topic_id: str | None = None) -> list[dict]:
    q = ("SELECT l.id, l.topic_id, l.title, l.minutes, l.blurb, t.title AS topic_title, "
         "(SELECT COUNT(*) FROM lesson_runs r WHERE r.lesson_id=l.id) AS runs "
         "FROM lessons l JOIN topics t ON t.id=l.topic_id")
    return rows(conn.execute(q + (" WHERE l.topic_id=?" if topic_id else "") + " ORDER BY l.id",
                             (topic_id,) if topic_id else ()))


def get_lesson(conn: sqlite3.Connection, lesson_id: str) -> dict:
    ls = one(conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)))
    if not ls:
        raise ValueError(f"no interactive lesson '{lesson_id}'")
    ls["steps"] = json.loads(ls["steps"])
    ls["topic"] = one(conn.execute("SELECT id, title, unit_id FROM topics WHERE id=?",
                                   (ls["topic_id"],)))
    return ls


def check_lesson_answer(qtype: str, answer: str, given: str, var: str | None = None,
                        choices: list | None = None) -> dict:
    """Stateless check for a lesson step (nothing recorded; the run is logged on completion)."""
    q = {"type": qtype, "answer": str(answer), "var": var, "tol": None,
         "choices": json.dumps(choices) if choices else None}
    r = grading.check(q, given)
    if r.get("unparseable"):
        return {"correct": None, "retry": True, "feedback": r["feedback"]}
    return {"correct": bool(r["correct"])}


def complete_lesson(conn: sqlite3.Connection, lesson_id: str, correct: int, total: int) -> dict:
    ls = get_lesson(conn, lesson_id)
    first = not conn.execute("SELECT 1 FROM lesson_runs WHERE lesson_id=?", (lesson_id,)
                             ).fetchone()
    conn.execute("INSERT INTO lesson_runs(lesson_id, correct, total, ts) VALUES(?,?,?,?)",
                 (lesson_id, int(correct), int(total), iso()))
    _touch(conn, ls["topic_id"])
    mark_studied(conn)
    award = award_xp(conn, (15 if first else 5) + 2 * max(0, int(correct)),
                     f"interactive lesson: {ls['title']}", ls["topic_id"])
    return {"lesson_id": lesson_id, "award": award, "first_time": first,
            "next": "Lock it in: try the topic's practice quiz unaided.",
            "achievements": check_achievements(conn)}
