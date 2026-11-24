"""The daily study pack for the reMarkable: one PDF per topic, in the order the tutor teaches.

  1. Try first   -- 1-2 attempt-first problems with room to work (before any explanation)
  2. The reading -- the assigned sections, cut from the learner's own book PDFs
  3. Recall      -- a blank page: main ideas from memory, what was confusing, a question

A pack holds at most `pack_pages` book pages (about half an hour of reading). A longer reading
goes out in parts, one per morning once most of the previous part has been read; the try-first
page opens part 1 and every part ends with a recall page.

Section -> page ranges come from curriculum/_reading_pages.yaml (scripts/index_readings.py).
Book PDFs are read from CALC_TUTOR_BOOKS_DIR. The generated pages are drawn at the
reMarkable's own size so handwriting space is real.
"""

from __future__ import annotations

import sqlite3

import io
import json
import logging
import math
import re
import time
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import yaml  # noqa: E402
from pypdf import PdfReader, PdfWriter  # noqa: E402

from . import config, engine  # noqa: E402
from .db import CURRICULUM_DIR, get_setting, load_books, rows, set_setting  # noqa: E402

log = logging.getLogger("calc_tutor.studypack")

PAGE_IN = (1404 / 226, 1872 / 226)  # reMarkable 2 screen, inches
INK = "#111111"
FAINT = "#9a9a9a"


class PackError(Exception):
    pass


def reading_pages() -> dict:
    p = CURRICULUM_DIR / "_reading_pages.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def primary_readings(readings: list[dict], books: dict, pages: dict) -> list[dict]:
    """The readings in the first of the learner's own books a topic lists. Readings in a second book
    cover the same ground (alternatives, not more homework), so they stay out of the pack."""
    local = [r for r in readings or []
             if books.get(r["book"], {}).get("local") and pages.get(r["book"], {}).get(str(r["sec"]))]
    return [r for r in local if r["book"] == local[0]["book"]] if local else []


def local_readings(topic: dict) -> list[dict]:
    """The topic's primary readings in the learner's own books, with PDF page ranges and pages."""
    books, pages = load_books(), reading_pages()
    out = []
    for r in primary_readings(topic.get("readings"), books, pages):
        book, ranges = books[r["book"]], pages[r["book"]][str(r["sec"])]
        path = config.books_dir() / book["local"].removeprefix("books/")
        out.append({**r, "book_title": book["title"], "path": path, "ranges": ranges,
                    "pages": [p for a, b in ranges for p in range(a, b + 1)]})
    return out


def split(readings: list[dict], budget: int) -> list[list[dict]]:
    """The readings in parts of at most `budget` pages. A reading stays whole when it fits;
    a longer one is cut into near-equal runs of pages."""
    out, cur, n = [], [], 0
    for r in readings:
        size = math.ceil(len(r["pages"]) / math.ceil(len(r["pages"]) / budget))
        for i in range(0, len(r["pages"]), size):
            chunk = r["pages"][i:i + size]
            if cur and n + len(chunk) > budget:
                out.append(cur)
                cur, n = [], 0
            cur.append({**r, "pages": chunk})
            n += len(chunk)
    return out + [cur] if cur else out


def _tex_safe(s: str) -> str:
    """Mathtext can't do everything LaTeX can; keep prompts renderable."""
    s = s.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    s = re.sub(r"\\text\{([^}]*)\}", r"\\mathrm{\1}", s)
    s = re.sub(r"\\(left|right)([.])", "", s)
    return s.replace("\\,", " ").replace("\\;", " ").replace("\\!", "")


def _wrap(s: str, width: int) -> list[str]:
    """Word-wrap without breaking inside $...$."""
    words, lines, cur = re.findall(r"\$[^$]*\$|\S+", s), [], ""
    for w in words:
        plain = len(re.sub(r"\\[a-zA-Z]+|[{}$^_]", "", w))
        if cur and len(re.sub(r"\\[a-zA-Z]+|[{}$^_]", "", cur)) + 1 + plain > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def _page() -> tuple[plt.Figure, plt.Axes]:
    fig = plt.figure(figsize=PAGE_IN)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    return fig, ax


def _text(ax, x, y, s, size=11, color=INK, weight="normal") -> float:
    """Draw (wrapped) text from the top at y; returns the y below it."""
    for line in _wrap(s, int(740 / size)):
        try:
            ax.text(x, y, _tex_safe(line), fontsize=size, color=color, weight=weight, va="top")
        except ValueError:  # mathtext could not parse it: show it raw
            ax.text(x, y, line.replace("$", ""), fontsize=size, color=color, weight=weight,
                    va="top")
        y -= size * 0.0024
    return y


def _rule(ax, y, label: str | None = None) -> None:
    ax.plot([0.07, 0.93], [y, y], color=FAINT, lw=0.6)
    if label:
        ax.text(0.07, y + 0.008, label, fontsize=8, color=FAINT)


def _pdf(fig) -> bytes:
    buf = io.BytesIO()
    try:
        fig.canvas.draw()
    except ValueError:
        pass  # a prompt mathtext rejects only fails at draw time; render_pages retries
    fig.savefig(buf, format="pdf")
    plt.close(fig)
    return buf.getvalue()


def try_first_page(topic: dict, day: date, questions: list[dict]) -> bytes:
    fig, ax = _page()
    y = 0.95
    ax.text(0.07, y, f"{topic['unit']['title']}  ·  {day:%a %b %-d}", fontsize=9, color=FAINT,
            va="top")
    y = _text(ax, 0.07, y - 0.025, topic["title"], size=20, weight="bold") - 0.01
    if questions:
        y = _text(ax, 0.07, y, "Try first. Before reading anything, have a go. Wrong is fine: "
                  "attempting first makes the reading stick.", size=10, color="#444444") - 0.02
        space = (y - 0.08) / len(questions)
        for i, q in enumerate(questions, 1):
            top = y
            y = _text(ax, 0.07, y, f"{i}.  {q['prompt']}", size=12)
            for j, choice in enumerate(json.loads(q.get("choices") or "[]")):
                y = _text(ax, 0.11, y - 0.004, f"({'abcdefgh'[j]})  {choice}", size=11)
            y = top - space
            _rule(ax, y + 0.01)
    else:
        y = _text(ax, 0.07, y, "Before you read: what do you already know about this? "
                  "Jot down anything -- words, pictures, guesses.", size=11) - 0.02
        _rule(ax, 0.1)
    ax.text(0.07, 0.035, "Ask your assistant: “check the problems on my reMarkable” when done.",
            fontsize=8, color=FAINT)
    return _pdf(fig)


def recall_page(topic: dict) -> bytes:
    fig, ax = _page()
    ax.text(0.07, 0.95, f"Recall  ·  {topic['title']}", fontsize=9, color=FAINT, va="top")
    _text(ax, 0.07, 0.92, "Close the book. In your own words, what are the main ideas?",
          size=14, weight="bold")
    _rule(ax, 0.40, "What was confusing or surprising?")
    _rule(ax, 0.18, "One question for your tutor")
    ax.text(0.07, 0.035, "Then ask your assistant: “look at my reMarkable” to check it against the "
            "key ideas.", fontsize=8, color=FAINT)
    return _pdf(fig)


def pretest_questions(conn: sqlite3.Connection, topic_id: str, n: int = 2) -> list[dict]:
    return rows(conn.execute(
        "SELECT id, type, prompt, choices FROM questions WHERE topic_id=? AND active=1 "
        "AND type IN ('mc','num','expr','antideriv') ORDER BY difficulty, id LIMIT ?",
        (topic_id, n)))


def build(conn: sqlite3.Connection, topic_id: str, day: date | None = None, part: int = 1) -> dict:
    """{"pdf": bytes, "title": str, "pages": int, "readings": [...], "part": k, "parts": n}
    for one part of a topic's pack."""
    day = day or engine.local_today()
    topic = engine.get_topic(conn, topic_id)
    readings = local_readings(topic)
    if not readings:
        raise PackError(f"No readings for {topic_id} in your own books")
    parts = split(readings, int(engine.setting(conn, "pack_pages")))
    if not 1 <= part <= len(parts):
        raise PackError(f"{topic_id} has {len(parts)} part(s), not {part}")
    writer = PdfWriter()

    def add(pdf: bytes | PdfReader, label: str, pages=None):
        reader = pdf if isinstance(pdf, PdfReader) else PdfReader(io.BytesIO(pdf))
        first = len(writer.pages)
        for i in pages or range(len(reader.pages)):
            writer.add_page(reader.pages[i])
        writer.add_outline_item(label, first)

    if part == 1:
        add(try_first_page(topic, day, pretest_questions(conn, topic_id)), "Try first")
    used, layout = [], []
    for r in parts[part - 1]:
        if not r["path"].exists():
            log.warning("book PDF missing: %s", r["path"])
            continue
        book = PdfReader(str(r["path"]))
        pages = [p - 1 for p in r["pages"] if p <= len(book.pages)]
        first = len(writer.pages) + 1
        add(book, f"{r['book']} {r['sec']}: {r['title']}", pages)
        # pack page -> book page, so reading in the pack counts as reading the book
        layout.append({"book": r["book"], "first": first,
                       "pages": [p + 1 for p in pages]})
        used.append({"book": r["book"], "sec": r["sec"], "title": r["title"],
                     "pages": f"{r['pages'][0]}-{r['pages'][-1]}"})
    if not used:
        raise PackError("The book PDFs for this topic are not on the Pi "
                        f"({config.books_dir()})")
    add(recall_page(topic), "Recall")
    writer.add_metadata({"/Title": topic["title"], "/Author": "Calculus Quest"})
    buf = io.BytesIO()
    writer.write(buf)
    of = f" ({part}/{len(parts)})" if len(parts) > 1 else ""
    return {"pdf": buf.getvalue(), "title": f"{day:%b %-d} · {topic['title']}{of}",
            "pages": len(writer.pages), "readings": used, "topic_id": topic_id,
            "layout": layout, "part": part, "parts": len(parts)}


# ------------------------------------------------------------------ delivery

FOLDER = "Calculus Quest"
_last_try = 0.0


def next_part(sent: dict | None) -> int | None:
    """The part of a topic's pack to send next (1 if none sent; None when all were sent)."""
    if not sent:
        return 1
    if sent.get("part", 0) < sent.get("parts", 0):
        return sent["part"] + 1
    return None


def part_read(conn: sqlite3.Connection, sent: dict) -> bool:
    """Most of the last part's book pages have been read on the device."""
    from .reading import DONE_FRACTION
    pages = [(x["book"], p) for x in sent.get("layout", []) for p in x["pages"]]
    seen = {(r[0], r[1]) for r in conn.execute("SELECT book, page FROM reading_pages")}
    return bool(pages) and sum(p in seen for p in pages) >= DONE_FRACTION * len(pages)


def due_topics(conn: sqlite3.Connection) -> list[str]:
    """Next topic in each course the learner has started (core if none) whose pack, or next part, is
    due: the first part when nothing was sent, a later part from the morning after the
    previous one once most of that was read. Respects the tutor's rule: no new topics while
    reviews are backlogged."""
    courses = [r[0] for r in conn.execute(
        "SELECT DISTINCT u.course FROM progress p JOIN topics t ON t.id=p.topic_id "
        "JOIN units u ON u.id=t.unit_id WHERE p.status!='new' ORDER BY u.course")] or ["core"]
    out = []
    for c in courses:
        nt = engine.next_topic(conn, c)
        if not nt.get("id") or nt.get("backlog"):
            continue
        sent = get_setting(conn, f"rm_pack:{nt['id']}")
        if not sent or (next_part(sent) and sent.get("date", "") < engine.local_today().isoformat()
                        and part_read(conn, sent)):
            out.append(nt["id"])
    return out


def push(conn: sqlite3.Connection, topic_id: str) -> dict:
    """Build the topic's next pack part (part 1 again once all were sent) and put it on the
    reMarkable now."""
    from . import remarkable

    sent = get_setting(conn, f"rm_pack:{topic_id}") or {}
    pack = build(conn, topic_id, part=next_part(sent) or 1)
    doc = remarkable.upload_pdf(pack["pdf"], pack["title"], FOLDER)
    # every part's layout stays, so reading in an earlier part still counts
    docs = {**sent.get("docs", {sent["doc"]: sent.get("layout", [])} if sent.get("doc") else {}),
            doc: pack["layout"]}
    set_setting(conn, f"rm_pack:{topic_id}",
                {"date": engine.local_today().isoformat(), "doc": doc, "title": pack["title"],
                 "layout": pack["layout"], "part": pack["part"], "parts": pack["parts"],
                 "docs": docs})
    steps = "try first, read, recall" if pack["part"] == 1 else "read, recall"
    engine.notify(conn, "pack", "📘 Study pack on your reMarkable",
                  f"{pack['title']} ({pack['pages']} pages): {steps}. "
                  f"It's in the “{FOLDER}” folder.")
    return {"title": pack["title"], "pages": pack["pages"], "readings": pack["readings"],
            "part": pack["part"], "parts": pack["parts"], "folder": FOLDER, "document_id": doc}


def daily(conn: sqlite3.Connection, now) -> None:
    """Scheduler hook: from the pack hour on, send the day's pack(s) the first time the device
    is awake and not mid-writing. Retries at most every 10 minutes."""
    global _last_try
    from . import remarkable, wellness

    if (not config.remarkable_host() or not engine.setting(conn, "remarkable_packs")
            or engine.setting(conn, "paused")
            or now.hour < int(engine.setting(conn, "remarkable_pack_hour"))):
        return
    if time.monotonic() - _last_try < 600:
        return
    topics = due_topics(conn)
    if not topics or wellness.today(conn).get("recovery_day"):
        return
    _last_try = time.monotonic()
    try:
        if remarkable.writing_recently():
            return
        for tid in topics:
            try:
                push(conn, tid)
            except PackError as e:
                log.info("no pack for %s: %s", tid, e)
                set_setting(conn, f"rm_pack:{tid}", {"skipped": str(e)})
    except remarkable.RemarkableError:
        pass  # asleep or away; try again later
