"""Reads the learner's reMarkable over SSH so the tutor can look at the page being written on.

The Pi keeps a mirror of the device's xochitl folder (metadata for every document, plus content,
ink and PDFs for pages that have been written on) and renders pages to PNG with Pillow (ink, via rmscene)
and pdftoppm (textbook pages). A poller watches for new ink so a look can highlight what was
written most recently. If CALC_TUTOR_REMARKABLE_HOST is unset nothing here touches the network.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shlex
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw
from rmscene import read_tree
from rmscene import scene_items as si

from . import config

log = logging.getLogger("calc_tutor.remarkable")
logging.getLogger("rmscene").setLevel(logging.ERROR)  # warns on every newer-format block

XOCHITL = "/home/root/.local/share/remarkable/xochitl"
SCREEN_W, SCREEN_H = 1404, 1872
SESSION_GAP = 15 * 60      # edits closer together than this are one writing session
POLL_SECONDS = 120
MAX_CAPTURES = 300
MAX_EDGE = 1568            # long edge of the full-page image sent to your assistant

_lock = threading.Lock()
_pdf_fetching: set[str] = set()


class RemarkableError(Exception):
    pass


# ------------------------------------------------------------------ device I/O


def _ssh(cmd: str, timeout: int = 30, data: bytes | None = None) -> bytes:
    host = config.remarkable_host()
    if not host:
        raise RemarkableError("reMarkable is not configured (CALC_TUTOR_REMARKABLE_HOST)")
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, cmd],
                           input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RemarkableError("reMarkable timed out") from e
    if r.returncode != 0:
        raise RemarkableError(f"reMarkable unreachable (asleep or off Wi-Fi?): "
                              f"{r.stderr.decode(errors='replace').strip()[:200]}")
    return r.stdout


def _mirror() -> Path:
    d = config.remarkable_dir() / "xochitl"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_dir() -> Path:
    d = config.remarkable_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _listing() -> dict[str, tuple[int, int]]:
    """{relative path: (mtime, size)} for every .metadata, .content and page .rm on the device."""
    out = _ssh(f"cd {XOCHITL} && stat -c '%Y %s %n' *.metadata *.content */*.rm 2>/dev/null; true")
    files = {}
    for line in out.decode(errors="replace").splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0].isdigit():
            files[parts[2]] = (int(parts[0]), int(parts[1]))
    return files


def _safe_member(name: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f-]{36}(\.(metadata|content)|/[0-9a-f-]{36}\.rm)", name))


def _fetch(paths: list[str], sizes: dict[str, tuple[int, int]] | None = None) -> None:
    """Copy the given device paths into the mirror, as tar streams over SSH in ~4 MB batches
    (the device drops a single large stream)."""
    batch, total = [], 0
    for p in paths:
        size = (sizes or {}).get(p, (0, 0))[1]
        if batch and (total + size > 4_000_000 or len(batch) >= 60):
            _fetch_batch(batch)
            batch, total = [], 0
        batch.append(p)
        total += size
    if batch:
        _fetch_batch(batch)


def _fetch_batch(paths: list[str]) -> None:
    data = _ssh(f"cd {XOCHITL} && tar cf - " + " ".join(shlex.quote(p) for p in paths),
                timeout=120)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for m in tf.getmembers():
            if not m.isfile() or not _safe_member(m.name):
                continue
            dest = _mirror() / m.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(tf.extractfile(m).read())


def _sync(listing: dict[str, tuple[int, int]], doc_ids: set[str]) -> None:
    """Refresh changed metadata for all documents, and content + ink for `doc_ids`."""
    want = []
    for path, (mtime, size) in listing.items():
        doc = path.split("/")[0].split(".")[0]
        if not path.endswith(".metadata") and doc not in doc_ids:
            continue
        local = _mirror() / path
        if not local.exists() or local.stat().st_size != size or local.stat().st_mtime < mtime:
            want.append(path)
            if path.endswith(".rm") and local.exists():
                _observe_file(local)  # the old copy is the "before" for new-ink highlighting
    _fetch(want, listing)
    # mirror the device mtime so unchanged files are skipped next time
    for path in want:
        local = _mirror() / path
        if local.exists():
            os.utime(local, (listing[path][0], listing[path][0]))
    # pages erased on the device: drop them from the mirror
    for doc in doc_ids:
        d = _mirror() / doc
        if d.is_dir():
            for f in d.glob("*.rm"):
                if f"{doc}/{f.name}" not in listing:
                    f.unlink()


def _download(name: str, dest: Path) -> None:
    """Stream one (large) device file to `dest`, atomically."""
    tmp = dest.with_suffix(".part")
    try:
        with open(tmp, "wb") as f:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                config.remarkable_host(), f"cat {XOCHITL}/{shlex.quote(name)}"],
                               stdout=f, timeout=1800)
        if r.returncode == 0 and tmp.stat().st_size > 0:
            tmp.rename(dest)
    finally:
        tmp.unlink(missing_ok=True)


def _ensure_pdf(doc: str) -> Path | None:
    """The document's PDF from the mirror, or None while a background download runs.
    Textbooks are hundreds of MB, so this never blocks a tool call."""
    pdf = _mirror() / f"{doc}.pdf"
    if pdf.exists():
        return pdf
    if doc in _pdf_fetching:
        return None
    _pdf_fetching.add(doc)

    def fetch():
        try:
            _download(f"{doc}.pdf", pdf)
        except (OSError, subprocess.TimeoutExpired):
            log.exception("PDF fetch failed for %s", doc)
        finally:
            _pdf_fetching.discard(doc)

    threading.Thread(target=fetch, name=f"rm-pdf-{doc[:8]}", daemon=True).start()
    return None


# ------------------------------------------------------------------ documents and pages


@dataclass
class Page:
    doc_id: str
    page_id: str
    doc_name: str
    file_type: str          # notebook | pdf | epub
    number: int             # 1-based: notebook page, or the PDF/EPUB page it annotates
    mtime: int
    template: str = ""
    pdf_page: int | None = None   # 0-based PDF page for the background


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _documents() -> dict[str, dict]:
    """Live (not deleted or trashed) documents in the mirror, keyed by id."""
    docs = {}
    for f in _mirror().glob("*.metadata"):
        m = _read_json(f)
        if m.get("deleted") or m.get("parent") == "trash" or m.get("type") != "DocumentType":
            continue
        docs[f.stem] = m
    return docs


def _page_index(content: dict) -> list[dict]:
    """Pages in reading order (handles content formatVersion 1 and 2)."""
    cp = content.get("cPages")
    if cp:
        pages = [p for p in cp.get("pages", []) if not p.get("deleted")]
        return sorted(pages, key=lambda p: (p.get("idx") or {}).get("value", ""))
    return [{"id": pid} for pid in content.get("pages", [])]


def _make_page(doc_id: str, page_id: str, mtime: int, docs: dict) -> Page | None:
    meta = docs.get(doc_id)
    if not meta:
        return None
    content = _read_json(_mirror() / f"{doc_id}.content")
    ftype = content.get("fileType") or "notebook"
    pages = _page_index(content)
    for i, p in enumerate(pages):
        if p["id"] == page_id:
            redir = (p.get("redir") or {}).get("value")
            pdf_page = redir if isinstance(redir, int) else (i if ftype == "pdf" else None)
            number = (pdf_page + 1) if ftype in ("pdf", "epub") and pdf_page is not None else i + 1
            return Page(doc_id, page_id, meta.get("visibleName", "?"), ftype, number, mtime,
                        (p.get("template") or {}).get("value", ""), pdf_page)
    return None


def _recent_pages(listing: dict[str, tuple[int, int]], docs: dict, limit: int) -> list[Page]:
    """Pages with ink, most recently written first."""
    out = []
    for path, (mtime, _) in sorted(listing.items(), key=lambda kv: -kv[1][0]):
        if not path.endswith(".rm"):
            continue
        doc_id, name = path.split("/")
        if doc_id not in docs:
            continue
        p = _make_page(doc_id, name[:-3], mtime, docs)
        if p:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match_document(query: str, docs: dict[str, dict]) -> str | None:
    """Best document id for a loose name ('scratch pad', 'kandel', 'stewart calc')."""
    q = _norm(query)
    if not q:
        return None
    best, best_score = None, 0.0
    for doc_id, m in docs.items():
        name = _norm(m.get("visibleName", ""))
        if not name:
            continue
        if name == q:
            score = 3.0
        elif q in name:
            score = 2.0 + len(q) / len(name)
        else:
            words = q.split()
            hits = sum(w in name for w in words)
            score = hits / len(words) if hits else 0
        score += int(m.get("lastModified") or 0) / 1e16  # tiebreak: most recently used
        if score > best_score:
            best, best_score = doc_id, score
    return best if best_score >= 0.5 else None


# ------------------------------------------------------------------ ink


def _walk(group: si.Group, out: list) -> None:
    for item_id in group.children.keys():
        item = group.children[item_id]
        if isinstance(item, si.Group):
            _walk(item, out)
        elif isinstance(item, si.Line):
            out.append((f"{item_id.part1}:{item_id.part2}", item))


@dataclass
class Ink:
    lines: list = field(default_factory=list)       # [(stroke id, si.Line)]
    highlights: list = field(default_factory=list)  # [si.GlyphRange]
    text: str = ""


def read_ink(path: Path) -> Ink:
    with open(path, "rb") as f:
        tree = read_tree(f)
    ink = Ink()
    _walk(tree.root, ink.lines)
    ink.lines = [(i, ln) for i, ln in ink.lines
                 if ln.tool not in (si.Pen.ERASER, si.Pen.ERASER_AREA) and ln.points]

    def glyphs(g):
        for k in g.children.keys():
            it = g.children[k]
            if isinstance(it, si.Group):
                glyphs(it)
            elif isinstance(it, si.GlyphRange):
                ink.highlights.append(it)
    glyphs(tree.root)
    if tree.root_text is not None:
        try:
            from rmscene.text import TextDocument
            doc = TextDocument.from_scene_item(tree.root_text)
            ink.text = "\n".join(str(p) for p in doc.contents).strip()
        except Exception:
            log.debug("typed text unreadable", exc_info=True)
    return ink


COLORS = {
    si.PenColor.BLACK: (0, 0, 0), si.PenColor.GRAY: (125, 125, 125),
    si.PenColor.WHITE: (255, 255, 255), si.PenColor.YELLOW: (250, 220, 30),
    si.PenColor.GREEN: (0, 160, 60), si.PenColor.PINK: (230, 80, 160),
    si.PenColor.BLUE: (30, 70, 220), si.PenColor.RED: (210, 30, 30),
    si.PenColor.GRAY_OVERLAP: (125, 125, 125), si.PenColor.HIGHLIGHT: (250, 220, 30),
}
HIGHLIGHTERS = {si.Pen.HIGHLIGHTER_1, si.Pen.HIGHLIGHTER_2, si.Pen.SHADER}


def _color(line) -> tuple[int, int, int]:
    if getattr(line, "color_rgba", None):
        return tuple(line.color_rgba[:3])
    return COLORS.get(line.color, (0, 0, 0))


def _bbox(lines) -> tuple[float, float, float, float] | None:
    xs = [p.x for _, ln in lines for p in ln.points]
    ys = [p.y for _, ln in lines for p in ln.points]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


# ------------------------------------------------------------------ rendering


def _pdf_background(pdf: Path, page0: int) -> Image.Image | None:
    """The PDF page scaled the way the device shows it (best fit, centred)."""
    try:
        info = subprocess.run(["pdfinfo", "-f", str(page0 + 1), "-l", str(page0 + 1), str(pdf)],
                              capture_output=True, text=True, timeout=60).stdout
        m = re.search(r"Page\s+\d+\s+size:\s+([\d.]+) x ([\d.]+)", info) or \
            re.search(r"Page size:\s+([\d.]+) x ([\d.]+)", info)
        w, h = (float(m.group(1)), float(m.group(2))) if m else (612.0, 792.0)
        scale_args = (["-scale-to-x", str(SCREEN_W), "-scale-to-y", "-1"]
                      if h / w <= SCREEN_H / SCREEN_W else
                      ["-scale-to-y", str(SCREEN_H), "-scale-to-x", "-1"])
        r = subprocess.run(["pdftoppm", "-f", str(page0 + 1), "-l", str(page0 + 1), "-png",
                            "-singlefile", *scale_args, str(pdf)], capture_output=True, timeout=120)
        if r.returncode != 0 or not r.stdout:
            return None
        return Image.open(io.BytesIO(r.stdout)).convert("RGB")
    except (OSError, subprocess.TimeoutExpired):
        log.exception("PDF render failed")
        return None


def render(ink: Ink, background: Image.Image | None = None,
           new_ids: set[str] | None = None) -> tuple[Image.Image, tuple | None]:
    """Draw the page. Returns (image, pixel bbox of the new ink or None)."""
    box = _bbox(ink.lines)
    height = max(SCREEN_H, int(box[3]) + 60) if box else SCREEN_H
    width = max(SCREEN_W, int(box[2] + SCREEN_W / 2) + 60) if box else SCREEN_W
    page = Image.new("RGB", (width, height), "white")
    if background is not None:
        page.paste(background, ((SCREEN_W - background.width) // 2, 0))
    ox = SCREEN_W / 2  # device x is centred on the page; y starts at the top

    marks = Image.new("RGBA", page.size, (0, 0, 0, 0))
    md = ImageDraw.Draw(marks)
    for g in ink.highlights:
        for r in g.rectangles:
            md.rectangle([r.x + ox, r.y, r.x + ox + r.w, r.y + r.h], fill=(250, 220, 30, 90))
    for _, ln in ink.lines:
        if ln.tool in HIGHLIGHTERS:
            pts = [(p.x + ox, p.y) for p in ln.points]
            w = max(8, int(sum(p.width for p in ln.points) / len(ln.points) / 4))
            md.line(pts, fill=(*_color(ln), 90), width=w, joint="curve")
    page = Image.alpha_composite(page.convert("RGBA"), marks).convert("RGB")

    d = ImageDraw.Draw(page)
    for _, ln in ink.lines:
        if ln.tool in HIGHLIGHTERS:
            continue
        pts = [(p.x + ox, p.y) for p in ln.points]
        w = max(1, round(sum(p.width for p in ln.points) / len(ln.points) / 4))
        color = _color(ln)
        if ln.tool in (si.Pen.PENCIL_1, si.Pen.PENCIL_2,
                       si.Pen.MECHANICAL_PENCIL_1, si.Pen.MECHANICAL_PENCIL_2):
            color = tuple(min(255, c + 60) for c in color)
        if len(pts) == 1:
            x, y = pts[0]
            d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=color)
        else:
            d.line(pts, fill=color, width=w, joint="curve")

    new_box = None
    if new_ids:
        nb = _bbox([(i, ln) for i, ln in ink.lines if i in new_ids])
        if nb:
            pad = 40
            new_box = (max(0, int(nb[0] + ox) - pad), max(0, int(nb[1]) - pad),
                       min(page.width, int(nb[2] + ox) + pad), min(page.height, int(nb[3]) + pad))
            d.rectangle(new_box, outline=(30, 120, 255), width=4)
    return page, new_box


def _png(img: Image.Image, max_edge: int = MAX_EDGE) -> bytes:
    if max(img.size) > max_edge:
        img = img.copy()
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------------ "what's new" tracking


def _history_path(page_id: str) -> Path:
    return _state_dir() / f"{page_id}.json"


def observe(page_id: str, mtime: int, ids: set[str]) -> dict:
    """Record the page's stroke ids if they changed. Keeps a short change history."""
    p = _history_path(page_id)
    h = _read_json(p) or {"changes": [], "looked": None}
    changes = h["changes"]
    if not changes or set(changes[-1][1]) != ids:
        changes.append([mtime, sorted(ids)])
        h["changes"] = changes[-40:]
        p.write_text(json.dumps(h))
    return h


def new_strokes(h: dict, ids: set[str]) -> set[str] | None:
    """Strokes from the latest writing session, or since the assistant last looked if that is later.
    None when there is no earlier state to compare with."""
    changes = h.get("changes", [])
    if len(changes) < 2:
        base_ts, base = None, None
    else:
        i = len(changes) - 1
        while i > 0 and changes[i][0] - changes[i - 1][0] <= SESSION_GAP:
            i -= 1
        base_ts, base = (changes[i - 1][0], set(changes[i - 1][1])) if i > 0 else (None, None)
    looked = h.get("looked")
    if looked and (base_ts is None or looked[0] >= base_ts) and set(looked[1]) != ids:
        base = set(looked[1])
    if base is None:
        return None
    return ids - base


def _observe_file(path: Path) -> None:
    try:
        ink = read_ink(path)
    except Exception:
        log.warning("unreadable page %s", path, exc_info=True)
        return
    observe(path.stem, int(path.stat().st_mtime), {i for i, _ in ink.lines})


def mark_looked(page_id: str, ids: set[str]) -> None:
    p = _history_path(page_id)
    h = _read_json(p) or {"changes": [], "looked": None}
    h["looked"] = [int(time.time()), sorted(ids)]
    p.write_text(json.dumps(h))


# ------------------------------------------------------------------ public API


def _save_capture(png: bytes, page: Page) -> Path:
    d = config.remarkable_dir() / "captures"
    d.mkdir(parents=True, exist_ok=True)
    slug = _norm(page.doc_name).replace(" ", "-")[:40] or page.doc_id[:8]
    path = d / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}-p{page.number}.png"
    path.write_bytes(png)
    for old in sorted(d.glob("*.png"))[:-MAX_CAPTURES]:
        old.unlink()
    return path


def _summary(p: Page) -> dict:
    return {"document": p.doc_name, "page": p.number, "type": p.file_type,
            "written": datetime.fromtimestamp(p.mtime, config.tz()).isoformat(timespec="minutes")}


def look(document: str | None = None, page: int | None = None,
         previous: bool = False) -> dict:
    """Render the most recently written page (or the one asked for).

    Returns {"info": {...}, "page_png": bytes, "new_png": bytes | None}."""
    with _lock:
        offline = None
        try:
            listing = _listing()
        except RemarkableError as e:
            offline = str(e)
            listing = _mirror_listing()
            if not listing:
                raise
        if not offline:
            _sync(listing, set())  # metadata for every document (names for matching)
        docs = _documents()

        if document:
            doc_id = match_document(document, docs)
            if not doc_id:
                raise RemarkableError(f"No document on the reMarkable matches '{document}'")
            doc_ids = {doc_id}
        else:
            doc_ids = set()
            for path, _ in sorted(listing.items(), key=lambda kv: -kv[1][0]):
                d = path.split("/")[0]
                if path.endswith(".rm") and d in docs:
                    doc_ids.add(d)
                if len(doc_ids) >= 5:
                    break
        if not offline:
            _sync(listing, doc_ids)
        candidates = [p for p in _recent_pages(listing, docs, 10_000) if p.doc_id in doc_ids]

        if page is not None:
            target = next((p for p in candidates if p.number == page), None)
            if target is None:
                raise RemarkableError(
                    f"No handwriting on page {page} of that document. Pages with ink: "
                    f"{sorted({p.number for p in candidates})[:40]}")
        elif not candidates:
            raise RemarkableError("No handwritten pages found")
        else:
            target = candidates[0]

        if previous:
            same_doc = sorted((p for p in candidates if p.doc_id == target.doc_id),
                              key=lambda p: p.number)
            before = [p for p in same_doc if p.number < target.number]
            if not before:
                raise RemarkableError("No earlier handwritten page in that document")
            target = before[-1]

        rm_path = _mirror() / target.doc_id / f"{target.page_id}.rm"
        ink = read_ink(rm_path)
        ids = {i for i, _ in ink.lines}
        h = observe(target.page_id, target.mtime, ids)
        fresh = new_strokes(h, ids)

        notes = []
        background = None
        if target.file_type == "pdf" and target.pdf_page is not None:
            pdf = (_mirror() / f"{target.doc_id}.pdf") if offline else _ensure_pdf(target.doc_id)
            if pdf and pdf.exists():
                background = _pdf_background(pdf, target.pdf_page)
            else:
                notes.append("The PDF behind this page is still downloading to the Pi; this "
                             "shows the handwriting only. Ask again in a minute or two.")
        elif target.file_type == "epub":
            notes.append("EPUB page backgrounds are not rendered; this shows the handwriting "
                         "only.")
        if offline:
            notes.append(f"reMarkable not reachable ({offline}); showing the last copy the Pi "
                         "synced.")

        img, box = render(ink, background, fresh)
        page_png = _png(img)
        new_png = None
        if box and (box[2] - box[0]) * (box[3] - box[1]) < 0.6 * img.width * img.height:
            new_png = _png(img.crop(box), max_edge=1400)
        mark_looked(target.page_id, ids)
        capture = _save_capture(page_png, target)

        info = {**_summary(target),
                "template": target.template or None,
                "strokes": len(ids),
                "new_strokes": (len(fresh) if fresh is not None else None),
                "new_ink": ("outlined in blue; a close-up follows the page" if new_png else
                            "outlined in blue" if box else
                            "no earlier snapshot to compare with" if fresh is None else
                            "nothing new since the last look"),
                "typed_text": ink.text or None,
                "capture": str(capture),
                "recent_pages": [_summary(p) for p in candidates[:6] if p is not target],
                "notes": notes}
        return {"info": info, "page_png": page_png, "new_png": new_png}


# ------------------------------------------------------------------ pushing documents


def _metadata(kind: str, name: str, parent: str) -> bytes:
    now = str(int(time.time() * 1000))
    return json.dumps({"createdTime": now, "lastModified": now, "lastOpened": "0",
                       "lastOpenedPage": 0, "parent": parent, "pinned": False, "type": kind,
                       "visibleName": name, "deleted": False, "metadatamodified": False,
                       "modified": False, "synced": False, "version": 0}, indent=4).encode()


def _folder_id(name: str) -> str | None:
    for f in _mirror().glob("*.metadata"):
        m = _read_json(f)
        if (m.get("type") == "CollectionType" and m.get("visibleName") == name
                and not m.get("deleted") and m.get("parent") != "trash"):
            return f.stem
    return None


def writing_recently(minutes: int = 10) -> bool:
    """True if any page got ink in the last `minutes` (device clock), i.e. the learner is mid-session."""
    out = _ssh(f"date +%s; cd {XOCHITL} && stat -c %Y */*.rm 2>/dev/null | sort -n | tail -n 1; "
               "true").decode().split()
    return len(out) >= 2 and int(out[0]) - int(out[1]) < minutes * 60


def upload_pdf(pdf: bytes, name: str, folder: str) -> str:
    """Put a PDF in `folder` (created if needed) on the device and restart the reading app so
    it shows up (the screen reloads for a few seconds). Returns the new document id."""
    with _lock:
        _sync(_listing(), set())
        files = {}
        fid = _folder_id(folder)
        if not fid:
            fid = str(uuid4())
            files[f"{fid}.metadata"] = _metadata("CollectionType", folder, "")
            files[f"{fid}.content"] = b"{}"
        doc = str(uuid4())
        files[f"{doc}.metadata"] = _metadata("DocumentType", name, fid)
        files[f"{doc}.content"] = json.dumps({"fileType": "pdf"}).encode()
        files[f"{doc}.pdf"] = pdf
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for path, data in files.items():
                info = tarfile.TarInfo(path)
                info.size = len(data)
                info.mtime = int(time.time())
                tf.addfile(info, io.BytesIO(data))
        _ssh(f"cd {XOCHITL} && tar xf - && systemctl restart xochitl", timeout=180,
             data=buf.getvalue())
        for path, data in files.items():  # the mirror already has them: no re-download
            (_mirror() / path).write_bytes(data)
        return doc


def _mirror_listing() -> dict[str, tuple[int, int]]:
    m = _mirror()
    files = {}
    for f in [*m.glob("*.metadata"), *m.glob("*.content"), *m.glob("*/*.rm")]:
        st = f.stat()
        files[str(f.relative_to(m))] = (int(st.st_mtime), st.st_size)
    return files


def poll() -> int:
    """Mirror recently written pages and snapshot their strokes (so a later look can tell what
    is new). Returns the number of pages observed. Quietly does nothing if the device is asleep."""
    if not config.remarkable_host():
        return 0
    with _lock:
        try:
            listing = _listing()
        except RemarkableError:
            return 0
        recent = sorted(((m, p) for p, (m, _) in listing.items() if p.endswith(".rm")),
                        reverse=True)[:3]
        docs_needed = {p.split("/")[0] for _, p in recent}
        changed = [(m, p) for m, p in recent
                   if not (_mirror() / p).exists() or (_mirror() / p).stat().st_mtime < m]
        # documents opened since last time: their content (page list) lets reading progress
        # map the open page to a book page
        opened = {p.split(".")[0] for p, (m, _) in listing.items() if p.endswith(".metadata")
                  and (not (_mirror() / p).exists() or (_mirror() / p).stat().st_mtime < m)}
        _sync(listing, (docs_needed if changed else set()) | opened)
        docs = _documents()
        n = 0
        for mtime, path in changed:
            _observe_file(_mirror() / path)
            n += 1
            doc_id = path.split("/")[0]
            content = _read_json(_mirror() / f"{doc_id}.content")
            if content.get("fileType") == "pdf" and doc_id in docs:
                _ensure_pdf(doc_id)
        return n


def _loop() -> None:
    while True:
        try:
            poll()
        except Exception:
            log.exception("reMarkable poll failed")
        time.sleep(POLL_SECONDS)


def start() -> None:
    if config.remarkable_host():
        threading.Thread(target=_loop, name="calc-tutor-remarkable", daemon=True).start()
