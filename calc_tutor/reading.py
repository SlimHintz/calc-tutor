"""Reading progress from the reMarkable, and handwritten recall answers.

The device records the page each document was last opened at. The reMarkable poller mirrors
that metadata; every scheduler tick this module turns new page positions into reading:

  study pack page  -> book page   (via the pack layout saved when it was pushed)
  textbook on device -> book page (matched to the learner's PDFs by page count)

Each position is logged (reading_log, the raw material for reading stats) and marks the book
page read (reading_pages); small forward jumps between two positions count the pages in between,
since the poller only sees the device every couple of minutes. A curriculum reading is done when
most of its pages were read and its last pages reached; when all of a topic's readings are
done, the learner gets a nudge to do the recall page.
"""

from __future__ import annotations

import sqlite3

import json
import logging
from functools import lru_cache

from . import config, engine
from .db import get_setting, iso, load_books, one, parse_iso, rows, set_setting

log = logging.getLogger("calc_tutor.reading")

GAP_FILL = 8           # a jump forward of up to this many pages counts the pages in between
GAP_MINUTES = 30
DONE_FRACTION = 0.6    # share of a section's pages that must have been seen


@lru_cache(maxsize=None)
def _page_count(path: str) -> int | None:
    try:
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    except Exception:
        return None


def local_books() -> dict[str, int]:
    """{book key: PDF page count} for the learner's own books on this machine."""
    out = {}
    for key, b in load_books().items():
        if b.get("local"):
            path = config.books_dir() / b["local"].removeprefix("books/")
            if path.exists() and (n := _page_count(str(path))):
                out[key] = n
    return out


def book_for(content: dict) -> str | None:
    """Which local book a device PDF is (page counts are assumed unique across the library)."""
    n = content.get("pageCount") or len((content.get("cPages") or {}).get("pages", []))
    if content.get("fileType") != "pdf" or not n:
        return None
    hits = [k for k, count in local_books().items() if count == n]
    return hits[0] if len(hits) == 1 else None


def _sections() -> list[tuple[str, str, str, int, int]]:
    """[(topic_id, book, sec, first page, last page)] for every reading a study pack carries."""
    from .db import load_curriculum
    from .studypack import primary_readings, reading_pages
    books, pages = load_books(), reading_pages()
    out = []
    for unit in load_curriculum():
        for t in unit.get("topics", []):
            for r in primary_readings(t.get("readings"), books, pages):
                for a, b in pages.get(r["book"], {}).get(str(r["sec"]), []):
                    out.append((t["id"], r["book"], str(r["sec"]), a, b))
    return out


def _resolve(doc: str, meta: dict, content: dict, packs: dict) -> tuple[str, int] | None:
    """(book, 1-based PDF page) the document is open at, or None (not a book page)."""
    from .remarkable import _page_index
    idx = meta.get("lastOpenedPage")
    if not isinstance(idx, int):
        return None
    if doc in packs:
        page = idx + 1
        for part in packs[doc]:
            if part["first"] <= page < part["first"] + len(part["pages"]):
                return part["book"], part["pages"][page - part["first"]]
        return None  # the try-first or recall page
    book = book_for(content)
    if not book:
        return None
    order = _page_index(content)
    redir = (order[idx].get("redir") or {}).get("value") if idx < len(order) else None
    return book, (redir if isinstance(redir, int) else idx) + 1


def _mark_pages(conn: sqlite3.Connection, doc: str, book: str, page: int, now: str) -> None:
    prev = one(conn.execute(
        "SELECT book, page, ts FROM reading_log WHERE doc_id=? ORDER BY id DESC LIMIT 1", (doc,)))
    pages = [page]
    if prev and prev["book"] == book and 0 < page - prev["page"] <= GAP_FILL:
        mins = (parse_iso(now) - parse_iso(prev["ts"])).total_seconds() / 60
        if mins <= GAP_MINUTES:
            pages = list(range(prev["page"] + 1, page + 1))
    conn.executemany("INSERT OR IGNORE INTO reading_pages(book, page, first_ts) VALUES(?,?,?)",
                     [(book, p, now) for p in pages])


def _check_done(conn: sqlite3.Connection, book: str, now: str) -> list[str]:
    """Mark readings in `book` done; returns topics whose readings are now all done."""
    seen = {r[0] for r in conn.execute("SELECT page FROM reading_pages WHERE book=?", (book,))}
    done = {(r[0], r[1], r[2]) for r in conn.execute("SELECT topic_id, book, sec FROM reading_done")}
    sections = _sections()
    finished = set()
    by_key: dict[tuple, list[tuple[int, int]]] = {}
    for tid, b, sec, a, z in sections:
        by_key.setdefault((tid, b, sec), []).append((a, z))
    for (tid, b, sec), ranges in by_key.items():
        if b != book or (tid, b, sec) in done:
            continue
        pages = [p for a, z in ranges for p in range(a, z + 1)]
        hit = sum(p in seen for p in pages)
        if hit >= DONE_FRACTION * len(pages) and any(p in seen for p in pages[-2:]):
            conn.execute("INSERT OR IGNORE INTO reading_done(topic_id, book, sec, done_at) "
                         "VALUES(?,?,?,?)", (tid, b, sec, now))
            finished.add(tid)
    complete = []
    for tid in finished:
        needed = {k for k in by_key if k[0] == tid}
        got = {(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT topic_id, book, sec FROM reading_done WHERE topic_id=?", (tid,))}
        if needed <= got:
            complete.append(tid)
    return complete


def tick(conn: sqlite3.Connection) -> int:
    """Process new page positions from the mirrored device metadata. Returns positions logged."""
    if not config.remarkable_host():
        return 0
    from .remarkable import _mirror, _read_json
    seen = get_setting(conn, "reading_seen", {})
    packs = {}
    for v in (json.loads(r[0]) for r in conn.execute(
            "SELECT value FROM settings WHERE key LIKE 'rm_pack:%'")):
        if isinstance(v, dict) and v.get("doc"):
            packs.update(v.get("docs") or {v["doc"]: v.get("layout", [])})
    now = iso()
    logged, books = 0, set()
    for f in _mirror().glob("*.metadata"):
        meta = _read_json(f)
        if meta.get("type") != "DocumentType" or meta.get("deleted"):
            continue
        doc = f.stem
        key = f"{meta.get('lastOpened')}:{meta.get('lastOpenedPage')}"
        first_sight = doc not in seen
        if seen.get(doc) == key:
            continue
        seen[doc] = key
        if first_sight and doc not in packs:
            continue  # where a book was left before we started watching is not new reading
        hit = _resolve(doc, meta, _read_json(_mirror() / f"{doc}.content"), packs)
        if not hit:
            continue
        book, page = hit
        _mark_pages(conn, doc, book, page, now)
        conn.execute("INSERT INTO reading_log(ts, local_date, doc_id, doc_name, book, page) "
                     "VALUES(?,?,?,?,?,?)", (now, engine.local_today().isoformat(), doc,
                                             meta.get("visibleName"), book, page))
        logged += 1
        books.add(book)
    set_setting(conn, "reading_seen", seen)
    for book in books:
        for tid in _check_done(conn, book, now):
            title = conn.execute("SELECT title FROM topics WHERE id=?", (tid,)).fetchone()
            engine.notify(conn, "pack", "📖 Reading done",
                          f"{title[0] if title else tid}: now the recall page, from memory. "
                          "Then ask your assistant to look at your reMarkable.")
    return logged


def progress(conn: sqlite3.Connection, topic_id: str) -> list[dict]:
    """Per local reading of the topic: pages read / total, done."""
    from .studypack import local_readings
    topic = engine.get_topic(conn, topic_id)
    done = {(r["book"], r["sec"]): r["done_at"] for r in rows(conn.execute(
        "SELECT book, sec, done_at FROM reading_done WHERE topic_id=?", (topic_id,)))}
    out = []
    for r in local_readings(topic):
        pages = [p for a, z in r["ranges"] for p in range(a, z + 1)]
        seen = {x[0] for x in conn.execute(
            "SELECT page FROM reading_pages WHERE book=? AND page BETWEEN ? AND ?",
            (r["book"], min(pages), max(pages)))}
        out.append({"book": r["book"], "sec": str(r["sec"]), "title": r["title"],
                    "pages_read": sum(p in seen for p in pages), "pages": len(pages),
                    "done": done.get((r["book"], str(r["sec"])))})
    return out


def save_recall(conn: sqlite3.Connection, topic_id: str, text: str, covered: list[str] | None = None,
                missed: list[str] | None = None, note: str = "",
                source: str = "remarkable") -> dict:
    engine._require_topic(conn, topic_id)
    cur = conn.execute(
        "INSERT INTO recalls(topic_id, ts, text, covered, missed, note, source) "
        "VALUES(?,?,?,?,?,?,?)", (topic_id, iso(), text.strip(),
                                  json.dumps(covered or []), json.dumps(missed or []), note,
                                  source))
    award = engine.award_xp(conn, 5, "recall from memory", topic_id)
    return {"recall_id": cur.lastrowid, "award": award}
