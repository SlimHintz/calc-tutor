from __future__ import annotations

import sqlite3

import hmac
import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import TextContent
from pydantic import Field
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import config, engine, grading, notify, oauth, reading, remarkable, studypack, wellness
from .auth import BearerAuthMiddleware, log_auth_event
from .db import connect, load_books, one, rows, set_setting
from .scheduler import send_pop_quiz

VERSION = "0.3.0"
STATIC = Path(__file__).parent / "static"

INSTRUCTIONS = """\
You are the learner's personal tutor. The learner is an adult relearning math after years away,
with a focus on data science / machine learning, and is also studying the neuroscience of learning
and memory. They are engineer-minded and a runner (running analogies land well). This server is
their learning record: courses, curriculum, progress, mastery, mistakes, spaced repetition, XP.

Courses (get_courses has each one's progress and teaching notes -- follow them):
- core: "Math for Data Science" -- precalc bootcamp, calculus (Stewart 7e), discrete math (Epp
  3e), LINEAR ALGEBRA (3Blue1Brown -> VMLS -> MML ch 2-4/Strang), PROBABILITY & STATISTICS
  (Blitzstein-Hwang, OpenIntro, ISL, Statistical Rethinking), matrix calculus, optimization.
- neuro: "Learning & Memory" -- Kandel 5e, science + apply it.

Start of every session: call get_student_dashboard. Note:
- diagnostic.taken false -> recommend the placement test first (web app /#/diagnostic).
- wellness.recovery_day true (short sleep / race / long run) -> reviews only today, no new topic.
- backlog true -> clear reviews before any new topic.
- learning_health: unaided delayed accuracy, hints per problem, calibration gap. Mention trends.

Session structure (evidence-based; keep to it):
1. Warm-up (5-10 min): due reviews + open mistakes. These are best done UNAIDED in the web app
   (/#/review); if done here, they answer before you say anything.
2. Mixed quiz (~5 min, web app /#/quiz?mode=mixed): interleaved look-alike problems, no labels.
3. New topic (20-30 min): start_topic, then 1-2 ATTEMPT-FIRST problems (get_quiz mode=pretest)
   BEFORE any explanation. Then the textbook reading does the explaining; you teach Socratically
   (predict -> try -> explain), one idea at a time, using concrete numbers first.
4. Independent practice (10-15 min): get_quiz on the topic; you are check-only.
5. Exit (5 min): log mistakes (with the misconception + whether they were confident), save notes.

Guardrails (enforced by tools; do not route around them):
- Quiz questions come without answers. Send their answer verbatim to check_answer (ask "sure or
  unsure?" first and pass confidence). Wrong answers do NOT reveal the solution.
- After a miss: let them retry; offer get_hint (ladder: nudge -> method -> worked example ->
  solution). reveal_solution only when they ask or are stuck after hints; then ask them to
  self-explain why it works and give the follow-up problem it returns.
- Never solve a quiz/practice problem for them before they have attempted it, even if they ask;
  explain that attempt-first is the deal. Explanations of CONCEPTS are always fine.
- Your own math can be wrong: verify derivations with compute / check_step before presenting
  them, and check each line of their multi-step work with check_step.
- New questions via add_question MUST include `verify` (a sympy expression the CAS evaluates);
  if the CAS disagrees, fix the question -- never show an unverified auto-graded problem.
- Completion is provisional. complete_topic when the key ideas are understood and a short quiz
  is >= ~70%. MASTERY is awarded automatically only from unaided, delayed evidence (3 different
  problems right without hints, one >= 3 days later or in a mixed/review quiz) -- don't promise it.
- Mistakes resolve automatically when they later solve a NEW problem from the topic unaided.
reMarkable: when they say "look at my reMarkable" (or ask about something they wrote), call
look_at_remarkable. It shows the page they wrote on most recently, anywhere on the device, with the
newest ink outlined in blue. Read their handwriting back to them briefly so they can correct a misread.
If the page is a practice/quiz problem, the guardrails above still apply (hints, not answers);
concept questions and notes from any subject get full explanations. Each morning the day's
study pack (try first -> reading -> recall page) lands in their "Calculus Quest" folder; when they
asks you to check it, look at those pages and grade the try-first answers with check_answer.
For the recall page: transcribe it, compare with the key ideas (get_topic), tell them what they got
and missed, then save_recall. Reading progress is tracked automatically (get_reading_progress).
Motivation: celebrate learning (fixed mistakes, delayed recall, mastery), not volume. No guilt
about missed days -- the rule is "never miss twice"; rest/race days are protected.
Interactive lessons: when a concept isn't clicking, call get_interactive_lessons and send them the
link (predict -> manipulate -> explain) before re-explaining in words.
Math formatting: LaTeX with $...$. Web app (reviews, quizzes, star map): {public_url}
"""

mcp = FastMCP("calc-tutor", instructions=INSTRUCTIONS.replace(
    "{public_url}", config.public_url() or "(set CALC_TUTOR_PUBLIC_URL)"))


def _run(fn: Callable, *args, **kwargs):
    with connect() as conn:
        out = fn(conn, *args, **kwargs)
    notify.kick()
    return out


def _err(e: Exception) -> dict:
    return {"error": str(e)}


def _safe(fn: Callable, *args, **kwargs):
    try:
        return _run(fn, *args, **kwargs)
    except (ValueError, grading.GradeError) as e:
        return _err(e)


# =============================== MCP tools ======================================


@mcp.tool
def get_student_dashboard() -> dict:
    """Call at the start of every session: level, consistency (weekly target / never-miss-twice
    chain), reviews due, backlog flag, next topics per course, learning health (the three numbers),
    wellness/recovery day, training block, last session notes, open mistakes, placement status."""
    def f(conn: sqlite3.Connection):
        d = engine.dashboard(conn)
        d.pop("xp_by_day", None)
        d.pop("study_days", None)
        d["open_mistakes_list"] = rows(conn.execute(
            "SELECT id, topic_id, what, misconception, confident, ts FROM mistakes "
            "WHERE resolved=0 ORDER BY confident DESC, ts DESC LIMIT 8"))
        d["web_app"] = config.public_url()
        return d
    return _run(f)


@mcp.tool
def get_courses() -> list[dict]:
    """All courses with progress, next topic, and the tutor_notes on how to teach each."""
    def f(conn: sqlite3.Connection):
        cs = engine.courses(conn)
        notes = dict(conn.execute("SELECT id, tutor_notes FROM courses").fetchall())
        for c in cs:
            c["tutor_notes"] = notes[c["id"]]
        return cs
    return _run(f)


@mcp.tool
def get_curriculum(course: Annotated[str | None, Field(
                       description="'core' or 'neuro'; omit for all courses")] = None,
                   unit_id: Annotated[str | None, Field(
                       description="e.g. 'U02', 'L01', 'S02'; omit for the whole pathway")] = None
                   ) -> list[dict]:
    """The learning pathway: units (constellations) and topics with status
    (new / learning / completed=provisional / mastered=proven), mastery 0-1, prereqs."""
    def f(conn: sqlite3.Connection):
        units = engine.curriculum(conn, course)
        if unit_id:
            units = [u for u in units if u["id"] == unit_id]
        else:
            for u in units:
                for t in u["topics"]:
                    t.pop("readings", None)
        return units
    return _run(f)


@mcp.tool
def get_textbooks() -> dict:
    """Book/video keys used in readings (e.g. STW 2.5 = Stewart section 2.5) with links.
    Books with `local` are PDFs the learner owns."""
    return load_books()


@mcp.tool
def look_at_remarkable(
        document: Annotated[str | None, Field(
            description="Part of a document name, e.g. 'scratch pad' or 'stewart'; omit for the "
                        "page written on most recently anywhere")] = None,
        page: Annotated[int | None, Field(
            description="Page number within that document (the PDF page for textbooks)")] = None,
        previous: Annotated[bool, Field(
            description="Show the handwritten page before it (for 'see previous page')")] = False,
) -> ToolResult:
    """Look at the learner's reMarkable tablet: renders the handwritten page (textbook page underneath for
    PDFs) as an image. New ink since their latest writing session -- or since you last looked -- is
    outlined in blue, with a close-up. Also lists their other recently written pages."""
    try:
        out = remarkable.look(document, page, previous)
    except remarkable.RemarkableError as e:
        return ToolResult(content=[TextContent(type="text", text=json.dumps(_err(e)))])
    content = [TextContent(type="text", text=json.dumps(out["info"])),
               Image(data=out["page_png"], format="png").to_image_content()]
    if out["new_png"]:
        content.append(Image(data=out["new_png"], format="png").to_image_content())
    return ToolResult(content=content)


@mcp.tool
def push_study_pack(topic_id: Annotated[str | None, Field(
        description="Topic to send; omit for the next topic in each course they have started")]
        = None) -> dict:
    """Send a study pack to the learner's reMarkable now: attempt-first problems, the assigned book
    sections (cut from their own PDFs), and a recall page, in the "Calculus Quest" folder. A
    reading longer than pack_pages goes out in parts; this sends the topic's next part. The
    tablet's screen reloads for a few seconds. Packs also go out automatically each morning."""
    def f(conn: sqlite3.Connection):
        ids = [topic_id] if topic_id else studypack.due_topics(conn)
        if not ids:
            nt = engine.next_topic(conn)
            if nt.get("backlog"):
                raise ValueError("Review backlog: clear due reviews before new topics")
            ids = [nt["id"]] if nt.get("id") else []
        if not ids:
            raise ValueError("No topic to send")
        return {"sent": [studypack.push(conn, t) for t in ids]}
    try:
        return _run(f)
    except (ValueError, studypack.PackError, remarkable.RemarkableError) as e:
        return _err(e)


@mcp.tool
def save_recall(topic_id: str,
                text: Annotated[str, Field(description="His recall, transcribed faithfully from "
                                           "the page (fix nothing)")],
                covered: Annotated[list[str] | None, Field(
                    description="Key ideas they got (short phrases)")] = None,
                missed: Annotated[list[str] | None, Field(
                    description="Key ideas missing or wrong")] = None,
                note: str = "") -> dict:
    """Record a from-memory recall (usually the recall page of a reMarkable study pack) after
    comparing it with the topic's key ideas. Missed ideas are good candidates for
    add_flashcard; wrong ones for log_mistake."""
    return _safe(reading.save_recall, topic_id, text, covered, missed, note)


@mcp.tool
def get_reading_progress(topic_id: Annotated[str | None, Field(
        description="omit for the topic in progress")] = None) -> dict:
    """How far they have read each of a topic's assigned sections on the reMarkable (pages read /
    total, done date). Updated automatically from the tablet's page positions."""
    def f(conn: sqlite3.Connection):
        tid = topic_id or engine.next_topic(conn).get("id")
        if not tid:
            raise ValueError("No topic in progress")
        return {"topic_id": tid, "readings": reading.progress(conn, tid)}
    return _safe(f)


@mcp.tool
def get_topic(topic_id: str) -> dict:
    """Lesson plan: summary, key ideas, readings, videos, DS connection, lab, practice question
    prompts (NO answers -- solutions unlock per attempt), mastery evidence, grades, mistakes."""
    return _safe(engine.get_topic, topic_id)


@mcp.tool
def start_topic(topic_id: str) -> dict:
    """Mark a topic as being learned. Then run 1-2 attempt-first problems (get_quiz
    mode='pretest') before teaching."""
    return _safe(engine.start_topic, topic_id)


@mcp.tool
def complete_topic(topic_id: str) -> dict:
    """Provisional completion (key ideas understood, short quiz >= ~70%): half XP, fun fact
    unlocked, cards enter spaced repetition. Mastery comes later from unaided delayed evidence."""
    return _safe(engine.complete_topic, topic_id)


@mcp.tool
def get_quiz(topic_id: str | None = None, n: int = 5,
             mode: Annotated[Literal["topic", "pretest", "mixed", "weak", "mistakes", "block"],
                             Field(description="topic: from topic_id; pretest: 1-2 attempt-first "
                                               "problems before teaching; mixed: interleaved "
                                               "look-alike types; weak; mistakes; block: "
                                               "cumulative training-block test")] = "topic"
             ) -> list[dict]:
    """Questions WITHOUT answers. Ask one at a time; grade with check_answer. For 'mixed' and
    'block', don't tell them which topic a problem is from (choosing the method is the point)."""
    return _run(engine.pick_questions, topic_id, n, mode)


@mcp.tool
def check_answer(question_id: int,
                 answer: Annotated[str, Field(description="His answer verbatim, e.g. '3x^2 + 2', "
                                                          "'pi/4', or a choice index")],
                 confidence: Annotated[Literal["sure", "unsure"] | None, Field(
                     description="Ask before checking: were they sure?")] = None,
                 correct: Annotated[bool | None, Field(
                     description="Only for 'free' questions after you've compared with the "
                                 "model answer: your verdict")] = None,
                 score: Annotated[float | None, Field(description="0-1 partial credit")] = None,
                 pretest: Annotated[bool, Field(description="true for attempt-first warm-up "
                                                            "problems (solution shown)")] = False
                 ) -> dict:
    """Grade an answer (symbolic equivalence / tolerance). Correct -> returns the solution.
    Wrong -> NO solution: retry, get_hint, or reveal_solution. Free response: first call returns
    the model answer for you to judge; call again with `correct`. retry=true means unparseable."""
    conf = {"sure": 1, "unsure": 0}.get(confidence or "")
    return _safe(engine.record_answer, question_id, answer, "pretest" if pretest else "assistant",
                 correct=correct, score=score, confidence=conf)


@mcp.tool
def get_hint(question_id: int, level: Annotated[int | None, Field(
                 ge=1, le=4, description="omit for the next rung")] = None,
             override: Annotated[bool, Field(description="hint before any attempt (recorded)")]
             = False) -> dict:
    """Hint ladder, unlocked after an attempt: 1 nudge, 2 method (answer masked), 3 similar
    worked example, 4 full solution. Every rung is logged (hints-per-problem metric)."""
    return _safe(engine.get_hint, question_id, level, override, "assistant")


@mcp.tool
def reveal_solution(question_id: int, override: bool = False) -> dict:
    """Full solution (hint level 4), only after an attempt. Returns a follow-up problem to try
    next and a self-explanation prompt. The problem is scheduled again soon."""
    return _safe(engine.get_hint, question_id, 4, override, "assistant")


@mcp.tool
def check_step(before: Annotated[str, Field(description="previous expression, e.g. '(x+1)^2'")],
               after: Annotated[str, Field(description="their/your next line, e.g. 'x^2+2x+1'")],
               var: Annotated[str, Field(description="variable(s), e.g. 'x' or 'x,y'")] = "x"
               ) -> dict:
    """Is `after` a valid rewrite of `before`? Use on each line of multi-step work (theirs and
    yours) -- step-level feedback is what makes tutoring work."""
    try:
        return grading.steps_equivalent(before, after, var)
    except grading.GradeError as e:
        return _err(e)


@mcp.tool
def compute(expression: Annotated[str, Field(
        description="sympy syntax, e.g. diff(x**3*exp(x), x), integrate(1/(1+x**2), (x, 0, 1)), "
                    "limit(sin(x)/x, x, 0), summation(1/2**k, (k, 0, oo)), "
                    "Matrix([[2,1],[1,2]]).eigenvals()")]) -> dict:
    """Evaluate with the CAS (sandboxed). Use it to verify your own derivations before
    presenting them, and to build `verify` strings for add_question."""
    try:
        return {"result": str(grading.cas_eval(expression))}
    except grading.GradeError as e:
        return _err(e)


@mcp.tool
def add_question(topic_id: str,
                 type: Annotated[Literal["mc", "num", "expr", "antideriv", "free"], Field(
                     description="mc: answer=0-based index; num: number; expr: expression in "
                                 "var; antideriv: any antiderivative; free: model answer text")],
                 prompt: Annotated[str, Field(description="Use $...$ LaTeX for math")],
                 answer: str,
                 verify: Annotated[str | None, Field(
                     description="REQUIRED for num/expr/antideriv: sympy expression whose value "
                                 "is the answer (antideriv: the integrand), e.g. "
                                 "diff(x**2*sin(x), x). Rejected if the CAS disagrees.")] = None,
                 explanation: str = "", choices: list[str] | None = None,
                 var: Annotated[str | None, Field(description="variable(s) for expr, e.g. 'x' "
                                                  "or 'x,y'")] = "x",
                 difficulty: Annotated[int, Field(ge=1, le=3)] = 1,
                 hint: str | None = None) -> dict:
    """Add a CAS-verified practice question (it also enters spaced repetition). Write a full
    worked solution in `explanation` -- it powers the hint ladder."""
    return _safe(engine.add_question, topic_id, type, prompt, answer, explanation, choices,
                 var, difficulty, hint, None, verify)


@mcp.tool
def add_flashcard(topic_id: str, front: str, back: str) -> dict:
    """Add a concept flashcard to spaced repetition. Use $...$ for math."""
    return _safe(engine.add_card, topic_id, front, back)


@mcp.tool
def get_due_reviews(limit: int = 10) -> list[dict]:
    """FSRS cards due now (respecting the daily new-card limit and review cap). Problem cards
    show a FRESH variant problem -- grade with check_answer; concept/mistake cards -> quiz them on
    `front`, then review_flashcard. Best done unaided in the web app."""
    return _run(engine.due_cards, limit)


@mcp.tool
def review_flashcard(card_id: int, rating: Annotated[int, Field(
        ge=1, le=4, description="1 again (forgot), 2 hard, 3 good, 4 easy")]) -> dict:
    """Record a flashcard review (FSRS scheduling)."""
    return _safe(engine.review_card, card_id, rating)


@mcp.tool
def record_grade(topic_id: str, score: Annotated[float, Field(ge=0, le=100)],
                 kind: Annotated[str, Field(description="quiz | exam | lab | explain-back")] = "quiz",
                 notes: str = "") -> dict:
    """Record a grade (0-100) for a topic, e.g. a lab write-up or an explain-back."""
    return _safe(engine.record_grade, topic_id, score, kind, notes)


@mcp.tool
def log_mistake(topic_id: str, what: Annotated[str, Field(description="What went wrong")],
                misconception: Annotated[str, Field(
                    description="The underlying wrong belief/gap -- the useful part")] = "",
                fix: str = "", question_id: int | None = None,
                confident: Annotated[bool, Field(
                    description="were they sure of the wrong answer? (prioritized)")] = False) -> dict:
    """Log a mistake. It becomes a spaced 'spot the trap' card; it resolves automatically once
    they solve a NEW problem from this topic unaided at least a day later."""
    return _safe(engine.log_mistake, topic_id, what, misconception, fix, question_id, confident)


@mcp.tool
def get_mistakes(topic_id: str | None = None, include_resolved: bool = False) -> list[dict]:
    """The mistake journal -- confident misses first."""
    def f(conn: sqlite3.Connection):
        q, a = "SELECT * FROM mistakes WHERE 1=1", []
        if topic_id:
            q, a = q + " AND topic_id=?", [topic_id]
        if not include_resolved:
            q += " AND resolved=0"
        return rows(conn.execute(q + " ORDER BY resolved, confident DESC, ts DESC LIMIT 50", a))
    return _run(f)


@mcp.tool
def resolve_mistake(mistake_id: int, note: str = "", override: bool = False) -> dict:
    """Resolve a mistake. Requires evidence (a later, unaided correct answer on a different
    problem from the topic); override=true forces it (recorded, less XP)."""
    return _safe(engine.resolve_mistake, mistake_id, note, override)


@mcp.tool
def save_session_notes(summary: str, topics: list[str] | None = None, next_steps: str = "",
                       mood: str = "") -> dict:
    """End-of-session journal so the next session (any device) picks up where you left off."""
    return _run(engine.add_journal, summary, topics, next_steps, mood)


@mcp.tool
def get_session_history(limit: int = 5) -> list[dict]:
    """Previous tutoring session notes, newest first."""
    return _run(lambda c: rows(c.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?",
                                         (limit,))))


@mcp.tool
def get_learning_health(days: int = 30) -> dict:
    """The three numbers that say whether learning is working: unaided delayed accuracy,
    hints per problem (dependence), calibration gap (overconfidence)."""
    return _run(engine.learning_health, days)


@mcp.tool
def get_progress_report() -> dict:
    """Per-unit mastery, weakest/strongest topics, accuracy, grades, learning health."""
    def f(conn: sqlite3.Connection):
        units = engine.curriculum(conn)
        studied = [t | {"unit": u["id"]} for u in units for t in u["topics"]
                   if t["status"] != "new"]
        acc = {r["topic_id"]: r for r in rows(conn.execute(
            "SELECT topic_id, COUNT(*) AS attempts, AVG(correct) AS accuracy, "
            "SUM(hints) AS hints FROM attempts WHERE channel NOT LIKE 'diag:%' "
            "GROUP BY topic_id"))}
        for t in studied:
            t.pop("readings", None)
            t.update(acc.get(t["id"], {"attempts": 0, "accuracy": None, "hints": 0}))
        return {
            "units": [{"id": u["id"], "title": u["title"], "course": u["course"],
                       "mastery": u["mastery"], "completed": u["completed"],
                       "mastered": u["mastered"], "topics": len(u["topics"])} for u in units],
            "weakest": sorted(studied, key=lambda t: t["mastery"])[:5],
            "strongest": sorted(studied, key=lambda t: -t["mastery"])[:5],
            "grades": rows(conn.execute("SELECT * FROM grades ORDER BY ts DESC LIMIT 20")),
            "learning_health": engine.learning_health(conn),
            "weekly": engine.weekly_summary(conn),
        }
    return _run(f)


@mcp.tool
def get_weekly_summary() -> dict:
    """This week's progress as a shareable one-liner (for an accountability friend)."""
    return _run(engine.weekly_summary)


@mcp.tool
def protect_day(day: Annotated[str | None, Field(description="YYYY-MM-DD; default today")] = None,
                reason: str = "rest day") -> dict:
    """Protect a day (rest, travel, race) so it never breaks the never-miss-twice chain."""
    return _safe(engine.protect_day, day, reason)


@mcp.tool
def get_interactive_lessons(topic_id: str | None = None) -> list[dict]:
    """Brilliant-style interactive lessons (predict -> manipulate -> explain) in the web app at
    /#/lesson/<id>. Suggest one when a concept isn't clicking, BEFORE re-explaining in words.
    They're guided practice: no mastery credit."""
    def f(conn: sqlite3.Connection):
        out = engine.lessons_for_topic(conn, topic_id)
        for x in out:
            x["url"] = f"{config.public_url()}/#/lesson/{x['id']}"
        return out
    return _run(f)


@mcp.tool
def get_fun_fact(topic_id: str | None = None) -> dict | None:
    """A fun fact or puzzle tied to a topic they've learned (or the fact of the day)."""
    def f(conn: sqlite3.Connection):
        if topic_id:
            return one(conn.execute("SELECT * FROM facts WHERE topic_id=? ORDER BY RANDOM() "
                                    "LIMIT 1", (topic_id,)))
        return engine.daily_fact(conn)
    return _run(f)


@mcp.tool
def get_achievements() -> list[dict]:
    """All achievements, with unlocked_at for earned ones."""
    return _run(engine.achievement_catalog)


@mcp.tool
def send_push(title: str, message: str) -> dict:
    """Send a push notification to the learner's phone. Use sparingly (one cue a day is the design)."""
    nid = _run(lambda c: engine.notify(c, "nudge", title, message, config.public_url() or None))
    return {"notification_id": nid, "push_configured": bool(config.ntfy_url())}


@mcp.tool
def update_settings(weekly_target: Annotated[int | None, Field(ge=1, le=7)] = None,
                    study_time: Annotated[str | None, Field(description="HH:MM local")] = None,
                    pop_quizzes_per_day: Annotated[int | None, Field(ge=0, le=1)] = None,
                    pop_window_start_hour: int | None = None,
                    pop_window_end_hour: int | None = None,
                    new_cards_per_day: Annotated[int | None, Field(ge=0, le=50)] = None,
                    review_cap: Annotated[int | None, Field(ge=10, le=300)] = None,
                    paused: bool | None = None, push_enabled: bool | None = None,
                    remarkable_packs: bool | None = None,
                    remarkable_pack_hour: Annotated[int | None, Field(ge=0, le=23)] = None,
                    pack_pages: Annotated[int | None, Field(
                        ge=4, le=60, description="Book pages per study pack part")] = None
                    ) -> dict:
    """Tune weekly target, the daily study cue time, opt-in pop quiz (0/1), review limits,
    vacation mode, and the daily reMarkable study pack (on/off, earliest hour, pages per
    part; longer readings go out in parts)."""
    return _safe(_update_settings, {k: v for k, v in locals().items()})


def _update_settings(conn: sqlite3.Connection, s: dict) -> dict:
    ints = {"weekly_target": (1, 7), "pop_quizzes_per_day": (0, 1), "new_cards_per_day": (0, 50),
            "review_cap": (10, 300), "pack_pages": (4, 60)}
    for k, (lo, hi) in ints.items():
        if s.get(k) is not None:
            set_setting(conn, k, max(lo, min(hi, int(s[k]))))
    if s.get("study_time"):
        h, m = str(s["study_time"]).split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError("study_time must be HH:MM")
        set_setting(conn, "study_time", f"{int(h):02d}:{int(m):02d}")
    if s.get("pop_window_start_hour") is not None or s.get("pop_window_end_hour") is not None:
        w = engine.setting(conn, "pop_window")
        start = s["pop_window_start_hour"] if s.get("pop_window_start_hour") is not None else w[0]
        end = s["pop_window_end_hour"] if s.get("pop_window_end_hour") is not None else w[1]
        if not 0 <= start < end <= 24:
            raise ValueError("need 0 <= start < end <= 24")
        set_setting(conn, "pop_window", [start, end])
    for k in ("paused", "push_enabled", "remarkable_packs"):
        if s.get(k) is not None:
            set_setting(conn, k, bool(s[k]))
    if s.get("remarkable_pack_hour") is not None:
        set_setting(conn, "remarkable_pack_hour", max(0, min(23, int(s["remarkable_pack_hour"]))))
    return _settings(conn)


def _settings(conn: sqlite3.Connection) -> dict:
    keys = ("weekly_target", "study_time", "pop_quizzes_per_day", "pop_window",
            "new_cards_per_day", "review_cap", "paused", "push_enabled", "remarkable_packs",
            "remarkable_pack_hour", "pack_pages")
    return {**{k: engine.setting(conn, k) for k in keys},
            "push_configured": bool(config.ntfy_url()),
            "running_data_connected": bool(wellness._api())}


@mcp.tool
def start_diagnostic() -> dict:
    """Start (or resume) the precalculus placement test (a pretest: solutions shown after each
    answer). Returns sections with questions (no answers) and progress."""
    return _run(engine.diag_start)


@mcp.tool
def answer_diagnostic(run_id: int, question_id: int, answer: str = "",
                      dont_know: Annotated[bool, Field(
                          description="true if they say they don't know -- never guess for them")
                      ] = False) -> dict:
    """Grade one placement answer (no XP, no review cards)."""
    return _safe(engine.diag_answer, run_id, question_id, answer, dont_know)


@mcp.tool
def finish_diagnostic(run_id: int) -> dict:
    """Score the placement test per skill area: strong (>=85%), review (60-85%), learn (<60%)."""
    return _safe(engine.diag_finish, run_id)


@mcp.tool
def apply_placement(run_id: int, section_ids: Annotated[list[str] | None, Field(
        description="sections to place out of; omit for all 'strong' ones")] = None) -> dict:
    """Mark the chosen sections' topics complete (with a confirmation review in 2 days)."""
    return _safe(engine.diag_apply, run_id, section_ids)


@mcp.tool
def get_diagnostic_results() -> dict:
    """Latest finished placement results and any run in progress."""
    def f(conn: sqlite3.Connection):
        st = engine.diag_state(conn, include_questions=False)
        return {"last_result": st["last_result"], "in_progress": st["run"]}
    return _run(f)


# ============================ Web UI REST API ====================================


def api(path: str, methods=("GET",)):
    def deco(fn):
        async def handler(request: Request):
            try:
                body = await request.json() if request.method == "POST" else {}
            except json.JSONDecodeError:
                body = {}
            try:
                with connect() as conn:
                    out = fn(conn, request, body)
                notify.kick()
                return JSONResponse(out)
            except (ValueError, KeyError, TypeError, grading.GradeError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        mcp.custom_route(path, methods=list(methods), include_in_schema=False)(handler)
        return fn
    return deco


@api("/api/dashboard")
def _dash(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.dashboard(conn)


@api("/api/curriculum")
def _curr(conn: sqlite3.Connection, req: Request, body: dict):
    return {"units": engine.curriculum(conn, req.query_params.get("course")),
            "books": load_books(), "courses": engine.courses(conn)}


@api("/api/topic/{topic_id}")
def _topic(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.get_topic(conn, req.path_params["topic_id"])


@api("/api/topic/{topic_id}/start", methods=("POST",))
def _start(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.start_topic(conn, req.path_params["topic_id"])


@api("/api/topic/{topic_id}/complete", methods=("POST",))
def _complete(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.complete_topic(conn, req.path_params["topic_id"])


@api("/api/quiz")
def _quiz(conn: sqlite3.Connection, req: Request, body: dict):
    p = req.query_params
    return engine.pick_questions(conn, p.get("topic"), int(p.get("n", 5)),
                                 p.get("mode", "topic"))


@api("/api/question/{qid:int}")
def _question(conn: sqlite3.Connection, req: Request, body: dict):
    q = engine.public_question(conn, req.path_params["qid"])
    if not q:
        raise ValueError("no such question")
    return q


@api("/api/answer", methods=("POST",))
def _answer(conn: sqlite3.Connection, req: Request, body: dict):
    channel = body.get("channel", "ui")
    if channel not in ("ui", "mixed", "review", "pop", "block", "pretest"):
        channel = "ui"
    return engine.record_answer(
        conn, int(body["question_id"]), str(body.get("answer", "")), channel=channel,
        notification_id=body.get("notification_id"), card_id=body.get("card_id"),
        correct=body.get("self_grade"), confidence=body.get("confidence"))


@api("/api/hint", methods=("POST",))
def _hint(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.get_hint(conn, int(body["question_id"]), body.get("level"),
                           bool(body.get("override")), "ui")


@api("/api/self_explain", methods=("POST",))
def _self_explain(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.save_self_explanation(conn, int(body["question_id"]), str(body.get("text", "")))


@api("/api/review")
def _review(conn: sqlite3.Connection, req: Request, body: dict):
    return {"cards": engine.due_cards(conn, int(req.query_params.get("n", 30))),
            "due": engine.due_count(conn)}


@api("/api/review/{card_id:int}", methods=("POST",))
def _review_card(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.review_card(conn, req.path_params["card_id"], int(body["rating"]))


@api("/api/mistakes")
def _mistakes(conn: sqlite3.Connection, req: Request, body: dict):
    return rows(conn.execute(
        "SELECT m.*, t.title AS topic_title FROM mistakes m JOIN topics t ON t.id=m.topic_id "
        "ORDER BY m.resolved, m.confident DESC, m.ts DESC LIMIT 200"))


@api("/api/mistake", methods=("POST",))
def _log_mistake(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.log_mistake(conn, body["topic_id"], body["what"], body.get("misconception", ""),
                              body.get("fix", ""), body.get("question_id"),
                              bool(body.get("confident")))


@api("/api/mistake/{mid:int}/resolve", methods=("POST",))
def _resolve(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.resolve_mistake(conn, req.path_params["mid"], body.get("note", ""),
                                  bool(body.get("override")))


@api("/api/achievements")
def _ach(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.achievement_catalog(conn)


@api("/api/facts")
def _facts(conn: sqlite3.Connection, req: Request, body: dict):
    return rows(conn.execute(
        "SELECT f.*, t.title AS topic_title FROM facts f JOIN topics t ON t.id=f.topic_id "
        "WHERE f.unlocked_at IS NOT NULL ORDER BY f.unlocked_at DESC"))


@api("/api/notifications")
def _notes(conn: sqlite3.Connection, req: Request, body: dict):
    return rows(conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 50"))


@api("/api/notifications/read", methods=("POST",))
def _notes_read(conn: sqlite3.Connection, req: Request, body: dict):
    conn.execute("UPDATE notifications SET read=1 WHERE read=0")
    return {"ok": True}


@api("/api/journal")
def _journal(conn: sqlite3.Connection, req: Request, body: dict):
    return rows(conn.execute("SELECT * FROM journal ORDER BY id DESC LIMIT 30"))


@api("/api/settings", methods=("GET", "POST"))
def _settings_api(conn: sqlite3.Connection, req: Request, body: dict):
    return _update_settings(conn, body) if req.method == "POST" else _settings(conn)


@api("/api/protect", methods=("POST",))
def _protect(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.protect_day(conn, body.get("day"), body.get("reason") or "rest day")


@api("/api/recovery", methods=("POST",))
def _recovery(conn: sqlite3.Connection, req: Request, body: dict):
    set_setting(conn, f"recovery_override:{engine.local_today().isoformat()}", bool(body.get("on")))
    return wellness.today(conn)


@api("/api/weekly_summary")
def _weekly(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.weekly_summary(conn)


@api("/api/learning_health")
def _health(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.learning_health(conn, int(req.query_params.get("days", 30)))


@api("/api/block")
def _block(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.block_info(conn)


@api("/api/block/complete", methods=("POST",))
def _block_complete(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.complete_block(conn, int(body["correct"]), int(body["total"]))


@api("/api/popquiz", methods=("POST",))
def _popquiz_now(conn: sqlite3.Connection, req: Request, body: dict):
    return {"notification_id": send_pop_quiz(conn)}


@api("/api/diagnostic")
def _diag(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.diag_state(conn)


@api("/api/diagnostic/start", methods=("POST",))
def _diag_start(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.diag_start(conn)


@api("/api/diagnostic/answer", methods=("POST",))
def _diag_answer(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.diag_answer(conn, int(body["run_id"]), int(body["question_id"]),
                              str(body.get("answer", "")), bool(body.get("dont_know")))


@api("/api/diagnostic/finish", methods=("POST",))
def _diag_finish(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.diag_finish(conn, int(body["run_id"]))


@api("/api/diagnostic/apply", methods=("POST",))
def _diag_apply(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.diag_apply(conn, int(body["run_id"]), body.get("section_ids"))


@api("/api/lessons")
def _lessons(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.lessons_for_topic(conn, req.query_params.get("topic"))


@api("/api/lesson/check", methods=("POST",))
def _lesson_check(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.check_lesson_answer(body["type"], body["answer"], body.get("given", ""),
                                      body.get("var"), body.get("choices"))


@api("/api/lesson/{lid}")
def _lesson(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.get_lesson(conn, req.path_params["lid"])


@api("/api/lesson/{lid}/complete", methods=("POST",))
def _lesson_done(conn: sqlite3.Connection, req: Request, body: dict):
    return engine.complete_lesson(conn, req.path_params["lid"], int(body.get("correct", 0)),
                                  int(body.get("total", 0)))


@api("/api/stats")
def _stats(conn: sqlite3.Connection, req: Request, body: dict):
    return {
        "by_channel": rows(conn.execute(
            "SELECT channel, COUNT(*) AS n, AVG(correct) AS acc FROM attempts GROUP BY channel")),
        "reviews": conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
        "cards_active": conn.execute("SELECT COUNT(*) FROM cards WHERE active=1").fetchone()[0],
    }


# ============================ Static UI + plumbing ================================


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def index(request: Request):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@mcp.custom_route("/static/{name}", methods=["GET"], include_in_schema=False)
async def static(request: Request):
    p = (STATIC / request.path_params["name"]).resolve()
    if p.parent != STATIC.resolve() or not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p)


@mcp.custom_route("/manifest.webmanifest", methods=["GET"], include_in_schema=False)
async def manifest(request: Request):
    return FileResponse(STATIC / "manifest.webmanifest",
                        media_type="application/manifest+json")


@mcp.custom_route("/favicon.svg", methods=["GET"], include_in_schema=False)
async def favicon(request: Request):
    return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": VERSION})


def _misconfigured() -> JSONResponse:
    return JSONResponse({"error": "server_error",
                         "error_description": "CALC_TUTOR_PUBLIC_URL is not configured"},
                        status_code=500)


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"],
                  include_in_schema=False)
async def oauth_metadata(request: Request) -> JSONResponse:
    base = config.public_url()
    if not base:
        return _misconfigured()
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"],
                  include_in_schema=False)
async def oauth_resource(request: Request) -> JSONResponse:
    base = config.public_url()
    if not base:
        return _misconfigured()
    return JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base]})


@mcp.custom_route("/oauth/register", methods=["POST"], include_in_schema=False)
async def oauth_register(request: Request) -> JSONResponse:
    body = await request.json()
    redirect_uris = body.get("redirect_uris") or []
    if not redirect_uris:
        return JSONResponse({"error": "invalid_client_metadata",
                             "error_description": "redirect_uris is required"}, status_code=400)
    with connect() as conn:
        result = oauth.register_client(conn, body.get("client_name"), redirect_uris)
    return JSONResponse(result, status_code=201)


@mcp.custom_route("/oauth/authorize", methods=["GET", "POST"], include_in_schema=False)
async def oauth_authorize(request: Request):
    params = request.query_params if request.method == "GET" else await request.form()
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    hidden = {
        "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
        "code_challenge": params.get("code_challenge", ""),
        "code_challenge_method": params.get("code_challenge_method", "S256"),
    }
    with connect() as conn:
        client = oauth.get_client(conn, client_id)
        if client is None or redirect_uri not in client["redirect_uris"]:
            return HTMLResponse("Unknown client or redirect_uri.", status_code=400)
        form = dict(client_name=client["client_name"], redirect_uri=redirect_uri,
                    hidden_fields=hidden)
        if request.method == "GET":
            return HTMLResponse(oauth.render_authorize_form(**form, error=None))
        expected = config.auth_token()
        if not expected or not hmac.compare_digest(params.get("token", ""), expected):
            log_auth_event("oauth authorize failure source_ip=%s",
                           request.client.host if request.client else "unknown")
            return HTMLResponse(oauth.render_authorize_form(
                **form, error="Incorrect token, try again."), status_code=401)
        code = oauth.issue_authorization_code(conn, client_id, redirect_uri,
                                              hidden["code_challenge"],
                                              hidden["code_challenge_method"])
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)


@mcp.custom_route("/oauth/token", methods=["POST"], include_in_schema=False)
async def oauth_token(request: Request) -> JSONResponse:
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    with connect() as conn:
        token, error = oauth.exchange_code_for_token(
            conn, form.get("code", ""), form.get("client_id", ""), form.get("redirect_uri", ""),
            form.get("code_verifier", ""))
    if error:
        return JSONResponse({"error": error}, status_code=400)
    return JSONResponse({"access_token": token, "token_type": "Bearer",
                         "expires_in": int(oauth.ACCESS_TOKEN_TTL.total_seconds())})


def build_app():
    return mcp.http_app(transport="streamable-http", middleware=[Middleware(BearerAuthMiddleware)])
