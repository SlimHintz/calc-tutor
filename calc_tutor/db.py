"""SQLite storage. Plain sqlite3 -- one short-lived connection per request/tool call."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import yaml

from . import config

CURRICULUM_DIR = Path(__file__).parent / "curriculum"

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses(
  id TEXT PRIMARY KEY, ord INTEGER, title TEXT, subtitle TEXT, color TEXT, blurb TEXT,
  tutor_notes TEXT);
CREATE TABLE IF NOT EXISTS units(
  id TEXT PRIMARY KEY, ord INTEGER, title TEXT, constellation TEXT, color TEXT,
  blurb TEXT, ds_why TEXT, course TEXT DEFAULT 'core');
CREATE TABLE IF NOT EXISTS topics(
  id TEXT PRIMARY KEY, unit_id TEXT REFERENCES units(id), ord INTEGER, title TEXT,
  summary TEXT, key_ideas TEXT, readings TEXT, videos TEXT, ds TEXT, lab TEXT,
  prereqs TEXT, xp INTEGER, optional INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS progress(
  topic_id TEXT PRIMARY KEY REFERENCES topics(id), status TEXT NOT NULL DEFAULT 'new',
  started_at TEXT, completed_at TEXT, last_activity TEXT);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, topic_id TEXT REFERENCES topics(id),
  type TEXT NOT NULL, prompt TEXT NOT NULL, choices TEXT, answer TEXT, var TEXT,
  tol REAL, explanation TEXT, hint TEXT, difficulty INTEGER DEFAULT 1,
  source TEXT DEFAULT 'seed', active INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY, question_id INTEGER, topic_id TEXT, given TEXT,
  correct INTEGER, score REAL, channel TEXT, ts TEXT);
CREATE INDEX IF NOT EXISTS attempts_topic ON attempts(topic_id, ts);
CREATE TABLE IF NOT EXISTS grades(
  id INTEGER PRIMARY KEY, topic_id TEXT, score REAL, kind TEXT, notes TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS mistakes(
  id INTEGER PRIMARY KEY, topic_id TEXT, question_id INTEGER, what TEXT,
  misconception TEXT, fix TEXT, resolved INTEGER DEFAULT 0, resolved_at TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS cards(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, topic_id TEXT, question_id INTEGER,
  front TEXT, back TEXT, ease REAL DEFAULT 2.5, interval REAL DEFAULT 0,
  reps INTEGER DEFAULT 0, lapses INTEGER DEFAULT 0, due TEXT, last_review TEXT,
  active INTEGER DEFAULT 0, created_at TEXT);
CREATE INDEX IF NOT EXISTS cards_due ON cards(active, due);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY, card_id INTEGER, rating INTEGER, ts TEXT);
CREATE TABLE IF NOT EXISTS xp_events(
  id INTEGER PRIMARY KEY, amount INTEGER, reason TEXT, topic_id TEXT, ts TEXT, local_date TEXT);
CREATE INDEX IF NOT EXISTS xp_date ON xp_events(local_date);
CREATE TABLE IF NOT EXISTS achievements(key TEXT PRIMARY KEY, unlocked_at TEXT);
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, topic_id TEXT, kind TEXT, title TEXT,
  body TEXT, answer TEXT, unlocked_at TEXT, seen INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS journal(
  id INTEGER PRIMARY KEY, ts TEXT, summary TEXT, topics TEXT, next_steps TEXT, mood TEXT);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, title TEXT, body TEXT, link TEXT,
  question_id INTEGER, pushed INTEGER DEFAULT 0, read INTEGER DEFAULT 0,
  answered INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS hint_events(
  id INTEGER PRIMARY KEY, question_id INTEGER, topic_id TEXT, level INTEGER,
  override INTEGER DEFAULT 0, channel TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS study_days(
  local_date TEXT PRIMARY KEY, studied INTEGER DEFAULT 0, protected INTEGER DEFAULT 0,
  reason TEXT);
CREATE TABLE IF NOT EXISTS interleave_groups(id INTEGER PRIMARY KEY, topics TEXT);
CREATE TABLE IF NOT EXISTS lessons(
  id TEXT PRIMARY KEY, topic_id TEXT, title TEXT, minutes INTEGER, blurb TEXT, steps TEXT);
CREATE TABLE IF NOT EXISTS lesson_runs(
  id INTEGER PRIMARY KEY, lesson_id TEXT, correct INTEGER, total INTEGER, ts TEXT);
CREATE TABLE IF NOT EXISTS diag_sections(
  id TEXT PRIMARY KEY, ord INTEGER, title TEXT, topics TEXT);
CREATE TABLE IF NOT EXISTS diag_runs(
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, results TEXT,
  applied INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS oauth_clients(id TEXT PRIMARY KEY, client_name TEXT, redirect_uris TEXT);
CREATE TABLE IF NOT EXISTS oauth_codes(
  code TEXT PRIMARY KEY, client_id TEXT, redirect_uri TEXT, code_challenge TEXT,
  code_challenge_method TEXT, expires_at TEXT, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS oauth_tokens(token TEXT PRIMARY KEY, client_id TEXT, expires_at TEXT);
CREATE TABLE IF NOT EXISTS recalls(
  id INTEGER PRIMARY KEY, topic_id TEXT, ts TEXT, text TEXT, covered TEXT, missed TEXT,
  note TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS reading_log(
  id INTEGER PRIMARY KEY, ts TEXT, local_date TEXT, doc_id TEXT, doc_name TEXT, book TEXT,
  page INTEGER);
CREATE TABLE IF NOT EXISTS reading_pages(
  book TEXT, page INTEGER, first_ts TEXT, PRIMARY KEY(book, page));
CREATE TABLE IF NOT EXISTS reading_done(
  topic_id TEXT, book TEXT, sec TEXT, done_at TEXT, PRIMARY KEY(topic_id, book, sec));
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _connect() -> sqlite3.Connection:
    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connect():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def one(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


# --- Curriculum sync ---------------------------------------------------------


def load_curriculum() -> list[dict]:
    units = []
    for f in sorted(CURRICULUM_DIR.glob("*.yaml")):
        if f.name.startswith("_"):
            continue
        units.append(yaml.safe_load(f.read_text()))
    return units


def load_courses() -> dict:
    return yaml.safe_load((CURRICULUM_DIR / "_courses.yaml").read_text())


def load_books() -> dict:
    return yaml.safe_load((CURRICULUM_DIR / "_books.yaml").read_text())


def sync_curriculum(conn: sqlite3.Connection) -> dict:
    """Upsert units/topics and seed questions/cards/facts by stable key.
    Content is refreshed on every start; learner state (progress, attempts,
    card scheduling) is never touched."""
    now = iso()
    counts = {"courses": 0, "units": 0, "topics": 0, "questions": 0, "cards": 0, "facts": 0}
    for ord_, (cid, c) in enumerate(load_courses().items()):
        conn.execute(
            "INSERT INTO courses(id, ord, title, subtitle, color, blurb, tutor_notes) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ord=excluded.ord, "
            "title=excluded.title, subtitle=excluded.subtitle, color=excluded.color, "
            "blurb=excluded.blurb, tutor_notes=excluded.tutor_notes",
            (cid, ord_, c["title"], c["subtitle"], c["color"], c["blurb"].strip(),
             c["tutor_notes"].strip()),
        )
        counts["courses"] += 1
    for u in load_curriculum():
        unit = u["unit"]
        conn.execute(
            "INSERT INTO units(id, ord, title, constellation, color, blurb, ds_why, course) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ord=excluded.ord, "
            "title=excluded.title, constellation=excluded.constellation, color=excluded.color, "
            "blurb=excluded.blurb, ds_why=excluded.ds_why, course=excluded.course",
            (unit["id"], unit["ord"], unit["title"], unit["constellation"], unit["color"],
             unit["blurb"].strip(), unit["ds_why"].strip(), unit.get("course", "core")),
        )
        counts["units"] += 1
        for ord_, t in enumerate(u["topics"]):
            conn.execute(
                "INSERT INTO topics(id, unit_id, ord, title, summary, key_ideas, readings, videos, "
                "ds, lab, prereqs, xp, optional) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET unit_id=excluded.unit_id, ord=excluded.ord, "
                "title=excluded.title, summary=excluded.summary, key_ideas=excluded.key_ideas, "
                "readings=excluded.readings, videos=excluded.videos, ds=excluded.ds, "
                "lab=excluded.lab, prereqs=excluded.prereqs, xp=excluded.xp, "
                "optional=excluded.optional",
                (t["id"], unit["id"], ord_, t["title"], t["summary"].strip(),
                 json.dumps(t.get("key_ideas", [])), json.dumps(t.get("readings", [])),
                 json.dumps(t.get("videos", [])), t.get("ds", "").strip(),
                 t.get("lab", "").strip(), json.dumps(t.get("prereqs", [])),
                 t.get("xp", 60), int(t.get("optional", False))),
            )
            conn.execute("INSERT OR IGNORE INTO progress(topic_id) VALUES(?)", (t["id"],))
            counts["topics"] += 1
            for i, q in enumerate(t.get("questions", [])):
                key = f"{t['id']}:q{i}"
                conn.execute(
                    "INSERT INTO questions(key, topic_id, type, prompt, choices, answer, var, tol, "
                    "explanation, hint, difficulty, source, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,'seed',?) ON CONFLICT(key) DO UPDATE SET "
                    "type=excluded.type, prompt=excluded.prompt, choices=excluded.choices, "
                    "answer=excluded.answer, var=excluded.var, tol=excluded.tol, "
                    "explanation=excluded.explanation, hint=excluded.hint, "
                    "difficulty=excluded.difficulty",
                    (key, t["id"], q["type"], q["q"].strip(),
                     json.dumps(q["choices"]) if "choices" in q else None,
                     str(q["a"]), q.get("var"), q.get("tol"),
                     (q.get("exp") or "").strip(), q.get("hint"), q.get("d", 1), now),
                )
                counts["questions"] += 1
            for i, c in enumerate(t.get("cards", [])):
                key = f"{t['id']}:c{i}"
                conn.execute(
                    "INSERT INTO cards(key, topic_id, front, back, created_at, kind) "
                    "VALUES(?,?,?,?,?,'concept') "
                    "ON CONFLICT(key) DO UPDATE SET front=excluded.front, back=excluded.back",
                    (key, t["id"], c["f"].strip(), c["b"].strip(), now),
                )
                counts["cards"] += 1
            for i, f in enumerate(t.get("fun", [])):
                key = f"{t['id']}:f{i}"
                conn.execute(
                    "INSERT INTO facts(key, topic_id, kind, title, body, answer) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, title=excluded.title, "
                    "body=excluded.body, answer=excluded.answer",
                    (key, t["id"], f["kind"], f["title"], f["body"].strip(),
                     (f.get("answer") or "").strip() or None),
                )
                counts["facts"] += 1
    return counts


def sync_diagnostic(conn: sqlite3.Connection) -> int:
    """Diagnostic questions live in the questions table with active=0 so they never appear
    in normal quizzes; key = diag:<section>:<i>."""
    path = CURRICULUM_DIR / "_diagnostic.yaml"
    if not path.exists():
        return 0
    spec = yaml.safe_load(path.read_text())
    set_setting(conn, "diagnostic_meta", {"title": spec["title"], "blurb": spec["blurb"].strip()})
    n = 0
    for ord_, sec in enumerate(spec["sections"]):
        conn.execute(
            "INSERT INTO diag_sections(id, ord, title, topics) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET ord=excluded.ord, title=excluded.title, "
            "topics=excluded.topics", (sec["id"], ord_, sec["title"], json.dumps(sec["topics"])))
        for i, q in enumerate(sec["questions"]):
            conn.execute(
                "INSERT INTO questions(key, topic_id, type, prompt, choices, answer, var, tol, "
                "explanation, hint, difficulty, source, active, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'diagnostic',0,?) ON CONFLICT(key) DO UPDATE SET "
                "topic_id=excluded.topic_id, type=excluded.type, prompt=excluded.prompt, "
                "choices=excluded.choices, answer=excluded.answer, var=excluded.var, "
                "tol=excluded.tol, explanation=excluded.explanation, difficulty=excluded.difficulty",
                (f"diag:{sec['id']}:{i}", sec["topics"][0], q["type"], q["q"].strip(),
                 json.dumps(q["choices"]) if "choices" in q else None, str(q["a"]), q.get("var"),
                 q.get("tol"), (q.get("exp") or "").strip(), None, q.get("d", 1), iso()))
            n += 1
    return n


# Columns added after the first release: (table, column, declaration)
MIGRATIONS = [
    ("units", "course", "TEXT DEFAULT 'core'"),
    ("cards", "fsrs", "TEXT"),
    ("cards", "kind", "TEXT DEFAULT 'concept'"),
    ("cards", "priority", "INTEGER DEFAULT 0"),
    ("cards", "first_review", "TEXT"),
    ("attempts", "aided", "INTEGER DEFAULT 0"),
    ("attempts", "hints", "INTEGER DEFAULT 0"),
    ("attempts", "revealed", "INTEGER DEFAULT 0"),
    ("attempts", "confidence", "INTEGER"),
    ("attempts", "first_try", "INTEGER DEFAULT 1"),
    ("attempts", "note", "TEXT"),
    ("questions", "verified", "INTEGER DEFAULT 1"),
    ("questions", "verify", "TEXT"),
    ("progress", "mastered_at", "TEXT"),
    ("mistakes", "card_id", "INTEGER"),
    ("mistakes", "confident", "INTEGER DEFAULT 0"),
    ("mistakes", "override", "INTEGER DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, decl in MIGRATIONS:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            continue  # fresh database: SCHEMA + this loop on the next start cover it
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def sync_interleave(conn: sqlite3.Connection) -> int:
    path = CURRICULUM_DIR / "_interleave.yaml"
    if not path.exists():
        return 0
    groups = yaml.safe_load(path.read_text())["groups"]
    conn.execute("DELETE FROM interleave_groups")
    for g in groups:
        conn.execute("INSERT INTO interleave_groups(topics) VALUES(?)", (json.dumps(g),))
    return len(groups)


def sync_lessons(conn: sqlite3.Connection) -> int:
    """Interactive lessons (curriculum/_lessons.yaml) are content: replaced wholesale on sync."""
    path = CURRICULUM_DIR / "_lessons.yaml"
    if not path.exists():
        return 0
    lessons = yaml.safe_load(path.read_text())["lessons"]
    conn.execute("DELETE FROM lessons")
    for ls in lessons:
        conn.execute("INSERT INTO lessons(id, topic_id, title, minutes, blurb, steps) "
                     "VALUES(?,?,?,?,?,?)", (ls["id"], ls["topic"], ls["title"],
                                             ls.get("minutes", 8), ls.get("blurb", ""),
                                             json.dumps(ls["steps"])))
    return len(lessons)


def init_db() -> dict:
    with connect() as conn:
        _migrate(conn)
        conn.executescript(SCHEMA)
        _migrate(conn)
        counts = sync_curriculum(conn)
        counts["diagnostic_questions"] = sync_diagnostic(conn)
        counts["interleave_groups"] = sync_interleave(conn)
        counts["lessons"] = sync_lessons(conn)
        # topics that disappeared from the curriculum (e.g. moved ids) keep their history but
        # questions from deleted YAML entries stop being served
        return counts
