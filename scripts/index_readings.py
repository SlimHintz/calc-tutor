"""Map every local-book reading in the curriculum ("STW 2.5", "BH 10.2-10.3", "KAN Ch 65") to
PDF page ranges, using each PDF's outline, or its running heads when it has no outline.

Run on a machine that has ~/calc-tutor/books (the Mac), then commit the output:
    .venv/bin/python scripts/index_readings.py
Writes calc_tutor/curriculum/_reading_pages.yaml as {book: {sec: [[first, last], ...]}} with
1-based PDF page numbers. Exercise and problem sets at the end of a section or chapter are left
out: the reading is the exposition, and practice comes from the tutor.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
CURRICULUM = ROOT / "calc_tutor" / "curriculum"
OUT = CURRICULUM / "_reading_pages.yaml"


def clean(t: str) -> str:
    return re.sub(r"\s+", " ", t.replace("\x00", "")).strip()


def outline(reader: PdfReader) -> list[tuple[int, str, int]]:
    """[(level, title, 1-based page)] in document order."""
    out = []

    def walk(items, level):
        for x in items:
            if isinstance(x, list):
                walk(x, level + 1)
            else:
                try:
                    out.append((level, clean(x.title), reader.get_destination_page_number(x) + 1))
                except Exception:
                    pass
    walk(reader.outline, 0)
    return out


def sec_pattern(token: str) -> re.Pattern:
    token = token.strip()
    if m := re.fullmatch(r"(?:Ch\.?|Chapter)\s*(\d+)", token, re.I):
        return re.compile(rf"^(?:Chapter\s+|Ch\.?\s*)?{m[1]}(?![\d.])\b", re.I)
    if m := re.fullmatch(r"App\.?\s*([A-Z])", token, re.I):
        return re.compile(rf"^(?:Appendix\s+{m[1]}\b|{m[1]}:\s)", re.I)
    if re.fullmatch(r"Diagnostic Test\s+[A-Z]", token, re.I):
        return re.compile(r"^Diagnostic Tests\b", re.I)  # all four share one outline entry
    return re.compile(rf"^(?:Section\s+)?{re.escape(token)}(?![\d])[:.]?\s")


def tokens(sec: str) -> list[list[str]]:
    """'4.2, 4.6' -> [['4.2'], ['4.6']]; '10.2-10.3' -> [['10.2', '10.3']]."""
    out = []
    for part in sec.split(","):
        ends = [p.strip() for p in part.split("-")]
        if len(ends) == 2 and not ends[1].lower().startswith(("ch", "app")) and \
                ends[0].lower().startswith(("ch", "chapter")):
            ends[1] = "Ch " + ends[1]
        out.append(ends)
    return out


def from_outline(entries, token: str) -> tuple[int, int] | None:
    pat = sec_pattern(token)
    for i, (level, title, page) in enumerate(entries):
        if pat.search(title):
            end = None
            for lv2, _, p2 in entries[i + 1:]:
                if lv2 <= level and p2 > page:
                    end = p2 - 1
                    break
                if lv2 <= level and p2 == page:  # next one starts on the same page
                    end = page
                    break
            return page, end or page + 20
    return None


def page_numbers(reader: PdfReader, contents: list[tuple[str, int]]) -> list[tuple[int, int]]:
    """[(printed page, PDF page)] observations, from running heads ('2 Chapter 1 ...',
    '1.1 Logical Form ... 3') and from chapter openers that list their sections ('2.1
    Introduction 2.2 ...'). Used to turn printed contents-page numbers into PDF pages."""
    obs = []
    chapters = dict(contents)
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        first = clean(text.split("\n", 1)[0])
        if m := re.match(r"^(\d+)\s+Chapter\s*\d", first):
            obs.append((int(m[1]), i))
        elif m := re.match(r"^\d+\.\d+\s+[A-Z].*\s(\d+)$", first):
            obs.append((int(m[1]), i))
        elif (m := re.match(r"^(\d+)\.1\s+Introduction\s+\1\.2\b", clean(text[:200]))) \
                and f"Ch {m[1]}" in chapters and i > 30:
            obs.append((chapters[f"Ch {m[1]}"], i))
    return obs


def to_pdf(obs: list[tuple[int, int]], printed: int) -> int | None:
    """PDF page for a printed page number, using the nearest observation's offset (books
    insert unnumbered pages, so a single global offset drifts)."""
    if not obs:
        return None
    near = sorted(obs, key=lambda o: abs(o[0] - printed))[:5]
    offsets = [pdf - pr for pr, pdf in near]
    off = max(set(offsets), key=offsets.count)
    return printed + off


def toc(reader: PdfReader) -> list[tuple[str, int]]:
    """[(label, printed page)] from the book's printed contents pages, in order; label is a
    section ('6.5') or a chapter ('Ch 4', from its 'N.1 Introduction' line)."""
    out = []
    for page in reader.pages[:30]:
        lines = [clean(x) for x in (page.extract_text() or "").split("\n")]
        for i, line in enumerate(lines):
            if not (m := re.match(r"^(\d+\.\d+)\s+\S", line)):
                continue
            # long titles wrap: the page number ends the next line instead
            for cont in [line, *lines[i + 1:i + 3]]:
                if cont is not line and re.match(r"^\d+\.\d+\s", cont):
                    break
                if n := re.search(r"\s(\d+)$", cont):
                    out.append((m[1], int(n[1])))
                    if m[1].endswith(".1"):
                        out.append(("Ch " + m[1].split(".")[0], int(n[1])))
                    break
    return out


# Pages the text layer can't locate (the opener is an image), as 1-based PDF pages.
OVERRIDES = {"ROSS": {"Ch 1": 15}}


def chapter_openers(reader: PdfReader, book: str) -> dict[int, int]:
    """{chapter: first PDF page} for books whose chapters open with a list of their sections
    ('4.1 Random Variables 4.2 Discrete Random Variables ...')."""
    found = {int(k.split()[1]): v for k, v in OVERRIDES.get(book, {}).items()}
    for i, page in enumerate(reader.pages, start=1):
        t = clean(page.extract_text() or "")[:300]
        if (m := re.match(r"^(\d+)\.1\s+\S.*?\s\1\.2\s", t)) and i > 12:
            found.setdefault(int(m[1]), i)
    return found


# A heading that starts a section's or chapter's exercises ("1.1 Exercises", "Exercise Set 1.1",
# "Problems", "12.6 Practice", "Self-Test Problems and Exercises").
EXERCISES = re.compile(r"^(?:\d+(?:\.\d+)*\s+)?(?:Exercises|Exercise Set \d+(?:\.\d+)*|Problems|"
                       r"Practice|Self-Test Problems.*|Theoretical Exercises)$"
                       # OpenStax: "SECTION 1.3 EXERCISES" (Elem./Interm. Algebra), "1.1 SECTION EXERCISES"
                       r"|^(?:SECTION \d+\.\d+ EXERCISES|\d+\.\d+ SECTION EXERCISES)$")


def trim_exercises(reader: PdfReader, first: int, last: int) -> int:
    """Last page of the reading proper: the page before the exercises start, or the page they
    start on when it still carries text of the section. Only the second half of a range is
    searched, so a mid-section heading or mention never cuts a reading short."""
    for p in range(first + (last - first) // 2, last + 1):
        lines = (reader.pages[p - 1].extract_text() or "").split("\n")
        for i, line in enumerate(lines):
            if EXERCISES.match(clean(line)):
                before = sum(len(x) for x in lines[:i])
                return p if before > 600 and p > first else max(first, p - 1)
    # Headings that are images (Ross): the problems are the run of numbered items ("16. Poker
    # dice ...", "3.79. Suppose ...") that ends the range; one page of long problems with few
    # numbers doesn't break the run. Its first page is kept when it opens with the summary.
    item = re.compile(r"(?m)^\s*(?:\d{1,2}\.)?\d{1,3}\.[ \t]+\S")

    def text(p: int) -> str:
        return reader.pages[p - 1].extract_text() or ""
    start, misses = None, 0
    for p in range(last, first + (last - first) // 2, -1):
        if len(item.findall(text(p))) >= 2:
            start, misses = p, 0
        elif misses or start is None:
            break
        else:
            misses += 1
    if start is None or start >= last - 1:
        return last
    m = item.search(text(start))
    return start if m and m.start() > 600 else start - 1


def from_openers(openers: dict[int, int], total: int, token: str):
    m = re.fullmatch(r"(?:Ch\.?|Chapter)\s*(\d+)", token)
    if not m or int(m[1]) not in openers:
        return None
    n = int(m[1])
    later = [p for c, p in openers.items() if c > n]
    return openers[n], (min(later) - 1 if later else total)


def from_toc(entries: list[tuple[str, int]], obs, token: str):
    token = re.sub(r"^(?:Chapter|Ch\.?)\s*", "Ch ", token)
    for i, (label, printed) in enumerate(entries):
        if label == token:
            chapter = token.startswith("Ch ")
            nxt = [p for lb, p in entries[i + 1:]
                   if lb.startswith("Ch ") == chapter and p > printed]
            end = (nxt[0] - 1) if nxt else printed + 15
            start, last = to_pdf(obs, printed), to_pdf(obs, end)
            return (start, last) if start and last else None
    return None


def main() -> int:
    books = yaml.safe_load((CURRICULUM / "_books.yaml").read_text())
    readings: dict[str, set[str]] = {}
    for f in sorted(CURRICULUM.glob("[a-z]*.yaml")):
        for unit in [yaml.safe_load(f.read_text())]:
            for t in unit.get("topics", []):
                for r in t.get("readings", []) or []:
                    if books.get(r["book"], {}).get("local"):
                        readings.setdefault(r["book"], set()).add(str(r["sec"]))
    result, missing = {}, []
    for book, secs in sorted(readings.items()):
        path = ROOT / books[book]["local"]
        reader = PdfReader(str(path))
        entries = outline(reader)
        use_outline = len(entries) > 30
        contents = [] if use_outline else toc(reader)
        obs = [] if use_outline else page_numbers(reader, contents)
        openers = {} if use_outline else chapter_openers(reader, book)
        result[book] = {}
        for sec in sorted(secs):
            ranges = []
            for group in tokens(sec):
                found = [from_outline(entries, t) if use_outline else
                         (from_openers(openers, len(reader.pages), t)
                          or from_toc(contents, obs, t)) for t in group]
                if not all(found):
                    missing.append(f"{book} {sec}")
                    ranges = []
                    break
                # up to the last section's exercises (a range keeps the exercises in between)
                ranges.append([found[0][0], trim_exercises(reader, *found[-1])])
            if ranges:
                result[book][sec] = ranges
        print(f"{book}: {len(result[book])}/{len(secs)} sections mapped", file=sys.stderr)
    OUT.write_text("# Generated by scripts/index_readings.py -- PDF pages (1-based) per reading.\n"
                   + yaml.safe_dump(result, sort_keys=True, default_flow_style=None))
    if missing:
        print("unmapped:", ", ".join(missing), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
