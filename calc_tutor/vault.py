"""Writes the learning record as an Obsidian vault folder (plain Markdown, $LaTeX$ renders).

    Home.md                  where things stand: level, streak, current topics, health
    Mistakes.md              open misconceptions by topic, and recently fixed ones
    Courses/<course>.md      the pathway, unit by unit, linking every topic
    Topics/<course>/<t>.md   one note per topic: status, key ideas, readings, prerequisites
                             (as links, so the graph view is the curriculum), your mistakes,
                             grades and session notes
    Log/YYYY-MM-DD.md        one note per study day: sessions, practice, mistakes, reMarkable
                             pages the assistant looked at (images in attachments/)

The folder is regenerated from the database -- edits inside it are overwritten -- so it lives
in its own subfolder of the vault and your own notes can link into it (e.g. sync the folder with rsync).
"""

from __future__ import annotations

import sqlite3

import json
import logging
import re
import shutil
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import config, engine
from .db import rows

log = logging.getLogger("calc_tutor.vault")

STATUS = {"new": "⚪ not started", "learning": "🟡 learning", "completed": "🟢 completed",
          "mastered": "⭐ mastered"}
EXPORT_EVERY = 15 * 60
_last_export = 0.0


def _name(s: str) -> str:
    """A safe note filename (Obsidian forbids * " \\ / < > : | ? # ^ [ ])."""
    s = re.sub(r"\s*:\s*", " – ", s)
    return re.sub(r'[*"\\/<>|?#^\[\]]+', "-", s).strip(" .-") or "untitled"


def _local(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    return dt.astimezone(config.tz()) if dt.tzinfo else dt


def _day(ts: str | None) -> str:
    dt = _local(ts)
    return dt.date().isoformat() if dt else ""


def _front(d: dict) -> str:
    lines = ["---"]
    for k, v in d.items():
        if isinstance(v, list):
            lines.append(f"{k}:" + "".join(f"\n  - {json.dumps(x, ensure_ascii=False)}"
                                            for x in v) if v else f"{k}: []")
        elif v is not None:
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    return "\n".join(lines + ["---", ""])


class _Notes:
    """Collects files, then writes only what changed and removes what no longer exists."""

    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, str | Path] = {}

    def add(self, rel: str, text: str) -> None:
        self.files[rel] = text.rstrip() + "\n"

    def attach(self, rel: str, src: Path) -> None:
        self.files[rel] = src

    def write(self) -> int:
        changed = 0
        for rel, content in self.files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, Path):
                if not p.exists() or p.stat().st_size != content.stat().st_size:
                    shutil.copyfile(content, p)
                    changed += 1
            elif not p.exists() or p.read_text() != content:
                p.write_text(content)
                changed += 1
        for p in sorted(self.root.rglob("*"), reverse=True):
            rel = str(p.relative_to(self.root))
            if p.is_file() and rel not in self.files:
                p.unlink()
                changed += 1
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        return changed


def export(conn: sqlite3.Connection, root: Path | None = None) -> dict:
    """Regenerate the vault folder. Returns {"notes": n, "changed": n, "path": str}."""
    root = root or config.vault_dir()
    root.mkdir(parents=True, exist_ok=True)
    notes = _Notes(root)
    books = {k: v.get("title", k) for k, v in _books().items()}
    courses = rows(conn.execute("SELECT id, title, subtitle FROM courses ORDER BY ord"))
    course_title = {c["id"]: c["title"] for c in courses}
    units = engine.curriculum(conn)
    detail = {t["id"]: t for t in rows(conn.execute(
        "SELECT t.id, t.summary, t.key_ideas, p.started_at, p.completed_at, p.last_activity "
        "FROM topics t JOIN progress p ON p.topic_id=t.id"))}

    # note names: topic title, disambiguated by course if two courses share a title
    topics = [(u, t) for u in units for t in u["topics"]]
    counts = defaultdict(int)
    for _, t in topics:
        counts[t["title"]] += 1
    link = {}
    for u, t in topics:
        title = t["title"] if counts[t["title"]] == 1 else \
            f"{t['title']} ({course_title.get(u['course'], u['course'])})"
        link[t["id"]] = _name(title)

    from .reading import _sections
    sections: dict[str, list] = defaultdict(list)  # topic -> [(book, sec, pages)]
    for tid, b, sec, a, z in _sections():
        sections[tid].append((b, sec, list(range(a, z + 1))))
    seen_pages = defaultdict(set)
    for b, p in conn.execute("SELECT book, page FROM reading_pages"):
        seen_pages[b].add(p)
    done = {(r[0], r[1], r[2]): r[3] for r in conn.execute(
        "SELECT topic_id, book, sec, done_at FROM reading_done")}
    recalls = defaultdict(list)
    for r in rows(conn.execute("SELECT * FROM recalls ORDER BY ts")):
        recalls[r["topic_id"]].append(r)

    mistakes = rows(conn.execute("SELECT * FROM mistakes ORDER BY ts DESC"))
    grades = rows(conn.execute("SELECT * FROM grades ORDER BY ts"))
    journal = rows(conn.execute("SELECT * FROM journal ORDER BY ts"))
    by_topic = defaultdict(lambda: {"mistakes": [], "grades": [], "journal": []})
    for m in mistakes:
        by_topic[m["topic_id"]]["mistakes"].append(m)
    for g in grades:
        by_topic[g["topic_id"]]["grades"].append(g)
    for j in journal:
        for tid in json.loads(j["topics"] or "[]"):
            by_topic[tid]["journal"].append(j)

    # ---- topics
    for u, t in topics:
        d = detail.get(t["id"], {})
        ideas = json.loads(d.get("key_ideas") or "[]")
        cname = course_title.get(u["course"], u["course"])
        body = [_front({"id": t["id"], "course": cname, "unit": u["title"],
                        "status": t["status"], "mastery": round(t["mastery"], 2),
                        "started": _day(d.get("started_at")) or None,
                        "completed": _day(d.get("completed_at")) or None,
                        "tags": ["tutor/topic", f"tutor/{t['status']}"]}),
                f"# {t['title']}", "",
                f"{STATUS.get(t['status'], t['status'])} · mastery {t['mastery']:.0%} · "
                f"[[{_name(cname)}]] › {u['title']}", ""]
        if d.get("summary"):
            body += [d["summary"].strip(), ""]
        if ideas:
            body += ["## Key ideas", *[f"- {i}" for i in ideas], ""]
        if t["prereqs"]:
            body += ["## Builds on", *[f"- [[{link[p]}]]" for p in t["prereqs"] if p in link],
                     ""]
        if t["readings"]:
            body += ["## Readings"]
            for r in t["readings"]:
                body.append(f"- {books.get(r['book'], r['book'])} §{r['sec']}: {r['title']}")
            body.append("")
        progress = {}
        for b, sec, pages in sections.get(t["id"], []):
            cur = progress.setdefault((b, sec), [0, 0])
            cur[0] += sum(p in seen_pages[b] for p in pages)
            cur[1] += len(pages)
        if any(n for n, _ in progress.values()):
            body += ["## Reading progress"]
            for (b, sec), (n, total) in progress.items():
                when = done.get((t["id"], b, sec))
                body.append(f"- {'✅' if when else '📖'} {books.get(b, b)} §{sec}: {n}/{total} "
                            f"pages" + (f" (done {_day(when)})" if when else ""))
            body.append("")
        for r in recalls.get(t["id"], []):
            body += [f"## Recall · {_day(r['ts'])}", "", *[f"> {x}" for x in
                                                           r["text"].splitlines()], ""]
            if covered := json.loads(r["covered"] or "[]"):
                body.append("Got: " + "; ".join(covered))
            if missed := json.loads(r["missed"] or "[]"):
                body.append("Missed: " + "; ".join(missed))
            if r.get("note"):
                body.append(r["note"])
            body.append("")
        bt = by_topic.get(t["id"])
        if bt and bt["mistakes"]:
            body += ["## My mistakes"]
            for m in bt["mistakes"]:
                mark = "✅" if m["resolved"] else "❌"
                body.append(f"- {mark} {_day(m['ts'])}: {m['what']}"
                            + (f" — *{m['misconception']}*" if m.get("misconception") else ""))
            body.append("")
        if bt and bt["grades"]:
            body += ["## Grades", *[f"- {_day(g['ts'])} {g['kind']}: {g['score']:.0f}%"
                                    + (f" — {g['notes']}" if g.get("notes") else "")
                                    for g in bt["grades"]], ""]
        if bt and bt["journal"]:
            body += ["## Session notes", *[f"- [[{_day(j['ts'])}]] {j['summary']}"
                                           for j in bt["journal"]], ""]
        notes.add(f"Topics/{_name(cname)}/{link[t['id']]}.md", "\n".join(body))

    # ---- courses
    for c in courses:
        body = [_front({"tags": ["tutor/course"]}), f"# {c['title']}", "",
                c.get("subtitle") or "", ""]
        for u in units:
            if u["course"] != c["id"]:
                continue
            body += [f"## {u['title']}", f"{u['completed']}/{len(u['topics'])} done · "
                     f"mastery {u['mastery']:.0%}", ""]
            body += [f"- {STATUS.get(t['status'], '')[:1]} [[{link[t['id']]}]]"
                     for t in u["topics"]] + [""]
        notes.add(f"Courses/{_name(c['title'])}.md", "\n".join(body))

    # ---- mistakes
    open_m = [m for m in mistakes if not m["resolved"]]
    fixed = [m for m in mistakes if m["resolved"]][:30]
    body = [_front({"tags": ["tutor/mistakes"]}), "# Mistakes", "",
            f"{len(open_m)} open · {sum(m['resolved'] for m in mistakes)} fixed", "",
            "## Open"]
    for tid, ms in _group(open_m).items():
        body.append(f"### [[{link.get(tid, tid)}]]")
        body += [f"- {m['what']}" + (f" — *{m['misconception']}*" if m.get("misconception")
                                    else "") + (" (I was sure)" if m.get("confident") else "")
                 for m in ms]
    body += ["", "## Recently fixed", *[f"- ✅ [[{link.get(m['topic_id'], m['topic_id'])}]]: "
                                        f"{m['what']} (fixed {_day(m['resolved_at'])})"
                                        for m in fixed]]
    notes.add("Mistakes.md", "\n".join(body))

    # ---- daily log
    days = _days(conn, journal, mistakes, grades, link)
    captures = config.remarkable_dir() / "captures"
    for f in sorted(captures.glob("*.png")) if captures.exists() else []:
        if m := re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}-(.+)-p(\d+)\.png", f.name):
            day = f"{m[1]}-{m[2]}-{m[3]}"
            notes.attach(f"attachments/{f.name}", f)
            days[day].append(f"### reMarkable {m[4]}:{m[5]} · {m[6].replace('-', ' ')} p{m[7]}"
                             f"\n![[{f.name}]]")
    for day, parts in days.items():
        notes.add(f"Log/{day}.md", "\n".join([_front({"tags": ["tutor/log"]}), f"# {day}", "",
                                              *parts]))

    # ---- home
    dash = engine.dashboard(conn)
    lvl, streak = dash.get("level", {}), dash.get("streak", {})
    current = [t for _, t in topics if t["status"] == "learning"]
    body = [_front({"tags": ["tutor/home"]}), "# Tutor", "",
            f"Level {lvl.get('level', '?')} · {streak.get('current', 0)}-day chain "
            f"(best {streak.get('best', 0)}) · {dash.get('due_reviews', 0)} reviews due · "
            f"{dash.get('topics_completed', 0)}/{dash.get('topics_total', 0)} topics done", "",
            "## Now", *([f"- [[{link[t['id']]}]] ({t['mastery']:.0%})" for t in current]
                        or ["- nothing in progress"]), "",
            "## Courses", *[f"- [[{_name(c['title'])}]]" for c in courses], "",
            "## Recent days", *[f"- [[{d}]]" for d in sorted(days, reverse=True)[:10]], "",
            f"[[Mistakes]] · {len(open_m)} open", "",
            f"*Updated {datetime.now(config.tz()):%Y-%m-%d %H:%M} from the tutor -- this folder "
            "is regenerated; keep your own notes outside it.*"]
    notes.add("Home.md", "\n".join(body))

    changed = notes.write()
    return {"notes": len(notes.files), "changed": changed, "path": str(root)}


def _books() -> dict:
    from .db import load_books
    return load_books()


def _group(ms: list[dict]) -> dict[str, list[dict]]:
    out = defaultdict(list)
    for m in ms:
        out[m["topic_id"]].append(m)
    return out


def _days(conn: sqlite3.Connection, journal, mistakes, grades, link) -> dict[str, list[str]]:
    days: dict[str, list[str]] = defaultdict(list)
    for j in journal:
        tl = ", ".join(f"[[{link.get(t, t)}]]" for t in json.loads(j["topics"] or "[]"))
        days[_day(j["ts"])].append("## Session\n" + (f"Topics: {tl}\n\n" if tl else "")
                                   + (j["summary"] or "")
                                   + (f"\n\n**Next:** {j['next_steps']}" if j.get("next_steps")
                                      else ""))
    practice = rows(conn.execute(
        "SELECT topic_id, ts, correct FROM attempts ORDER BY ts"))
    per = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for a in practice:
        c = per[_day(a["ts"])][a["topic_id"]]
        c[0] += 1
        c[1] += bool(a["correct"])
    for day, tops in per.items():
        days[day].append("## Practice\n" + "\n".join(
            f"- [[{link.get(t, t)}]]: {ok}/{n} right" for t, (n, ok) in tops.items()))
    for g in grades:
        days[_day(g["ts"])].append(f"- 📝 {g['kind']} [[{link.get(g['topic_id'], g['topic_id'])}]]"
                                   f": {g['score']:.0f}%")
    for m in mistakes:
        days[_day(m["ts"])].append(f"- ❌ [[{link.get(m['topic_id'], m['topic_id'])}]]: "
                                   f"{m['what']}")
    pages_by_day = defaultdict(lambda: defaultdict(int))
    for b, ts in conn.execute("SELECT book, first_ts FROM reading_pages"):
        pages_by_day[_day(ts)][b] += 1
    books = _books()
    for day, per_book in pages_by_day.items():
        days[day].append("## Reading\n" + "\n".join(
            f"- {books.get(b, {}).get('title', b)}: {n} new pages" for b, n in per_book.items()))
    for r in rows(conn.execute("SELECT topic_id, ts FROM recalls")):
        days[_day(r["ts"])].append(f"- 🧠 recall: [[{link.get(r['topic_id'], r['topic_id'])}]]")
    days.pop("", None)
    return days


def tick(conn: sqlite3.Connection) -> None:
    """Scheduler hook: re-export at most every 15 minutes."""
    global _last_export
    if time.monotonic() - _last_export < EXPORT_EVERY:
        return
    _last_export = time.monotonic()
    try:
        export(conn)
    except Exception:
        log.exception("vault export failed")
