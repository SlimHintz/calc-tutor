import io
import json
from datetime import datetime

import pytest
from fastmcp import Client
from pypdf import PdfReader, PdfWriter
from test_remarkable import device, make_rm  # noqa: F401  (fixture)

from calc_tutor import studypack
from calc_tutor.db import connect, get_setting
from calc_tutor.server import mcp


@pytest.fixture
def books(tmp_path, monkeypatch):
    """A 6-page stand-in for Stewart, with section 2.5 on PDF pages 2-3."""
    d = tmp_path / "books"
    (d / "calculus").mkdir(parents=True)
    w = PdfWriter()
    for _ in range(6):
        w.add_blank_page(612, 792)
    with open(d / "calculus" / "stewart-calculus-7e-2012.pdf", "wb") as f:
        w.write(f)
    monkeypatch.setenv("CALC_TUTOR_BOOKS_DIR", str(d))
    monkeypatch.setattr(studypack, "reading_pages", lambda: {"STW": {"2.5": [[2, 3]]}})
    return d


def test_build_orders_try_first_reading_recall(books):
    with connect() as conn:
        pack = studypack.build(conn, "d-chain")
    r = PdfReader(io.BytesIO(pack["pdf"]))
    assert pack["pages"] == len(r.pages) == 4  # try first + 2 book pages + recall
    assert [o.title for o in r.outline] == ["Try first", "STW 2.5: The Chain Rule", "Recall"]
    assert "Differentiate" in r.pages[0].extract_text()  # an attempt-first problem
    assert "own words" in r.pages[-1].extract_text()
    assert pack["title"].endswith("The Chain Rule")


def test_build_needs_a_local_reading(books):
    with connect() as conn, pytest.raises(studypack.PackError):
        studypack.build(conn, "dm-markov")  # only online readings


def test_the_real_index_covers_every_local_reading():
    from calc_tutor.db import load_books, load_curriculum
    books, pages = load_books(), studypack.reading_pages()
    for unit in load_curriculum():
        for t in unit.get("topics", []):
            for r in t.get("readings") or []:
                if books.get(r["book"], {}).get("local"):
                    assert pages.get(r["book"], {}).get(str(r["sec"])), (r["book"], r["sec"])


def test_daily_push_once_when_idle(device, books, monkeypatch):  # noqa: F811
    monkeypatch.setattr(studypack, "due_topics", lambda conn: ["d-chain"])
    with connect() as conn:
        device.clock += 3600  # nothing written for an hour
        studypack.daily(conn, datetime(2026, 10, 20, 4))
        assert getattr(device, "restarts", 0) == 0  # before the pack hour
        studypack.daily(conn, datetime(2026, 10, 20, 6))
        sent = get_setting(conn, "rm_pack:d-chain")
    assert sent and device.restarts == 1
    meta = json.loads((device.root / f"{sent['doc']}.metadata").read_text())
    folder = json.loads((device.root / f"{meta['parent']}.metadata").read_text())
    assert folder["visibleName"] == "Calculus Quest" and folder["type"] == "CollectionType"
    assert (device.root / f"{sent['doc']}.pdf").stat().st_size > 1000
    assert json.loads((device.root / f"{sent['doc']}.content").read_text())["fileType"] == "pdf"


def test_daily_waits_while_writing(device, books, monkeypatch):  # noqa: F811
    monkeypatch.setattr(studypack, "due_topics", lambda conn: ["d-chain"])
    device.add_doc("scratch pad", [make_rm([[(-400, 200), (0, 200)]])])  # ink just now
    with connect() as conn:
        studypack.daily(conn, datetime(2026, 10, 20, 6))
        assert get_setting(conn, "rm_pack:d-chain") is None


async def test_push_tool(device, books):  # noqa: F811
    async with Client(mcp) as c:
        out = (await c.call_tool("push_study_pack", {"topic_id": "d-chain"})).data
    assert "sent" in out, out
    assert out["sent"][0]["pages"] == 4 and out["sent"][0]["folder"] == "Calculus Quest"


def test_split_keeps_short_readings_whole_and_cuts_long_ones_evenly():
    r = lambda sec, n: {"sec": sec, "pages": list(range(1, n + 1))}  # noqa: E731
    parts = studypack.split([r("a", 5), r("b", 6), r("c", 26)], 12)
    assert [[(x["sec"], len(x["pages"])) for x in p] for p in parts] == \
        [[("a", 5), ("b", 6)], [("c", 9)], [("c", 9)], [("c", 8)]]


def test_only_the_first_books_readings_go_in_the_pack():
    books = {"STW": {"local": "x"}, "PRE": {"local": "y"}, "OS1": {}}
    pages = {"STW": {"1.1": [[1, 2]], "1.3": [[5, 6]]}, "PRE": {"2.1": [[9, 9]]}}
    rs = [{"book": "OS1", "sec": "1"}, {"book": "STW", "sec": "1.1"},
          {"book": "PRE", "sec": "2.1"}, {"book": "STW", "sec": "1.3"}]
    assert [r["sec"] for r in studypack.primary_readings(rs, books, pages)] == ["1.1", "1.3"]


def test_long_reading_goes_out_in_parts_as_each_is_read(device, books, monkeypatch):  # noqa: F811
    from calc_tutor import engine
    from calc_tutor.db import set_setting
    nt = {"id": "d-chain"}
    monkeypatch.setattr(engine, "next_topic", lambda conn, course=None: nt)
    with connect() as conn:
        set_setting(conn, "pack_pages", 1)
        assert studypack.due_topics(conn) == ["d-chain"]
        first = studypack.push(conn, "d-chain")
        assert (first["part"], first["parts"], first["pages"]) == (1, 2, 3)  # try first + 1 + recall
        sent = get_setting(conn, "rm_pack:d-chain")
        assert studypack.due_topics(conn) == []  # same day, and not read yet
        set_setting(conn, "rm_pack:d-chain", {**sent, "date": "2000-01-01"})
        assert studypack.due_topics(conn) == []  # a day later, still not read
        conn.execute("INSERT INTO reading_pages(book, page, first_ts) VALUES('STW', 2, 'x')")
        assert studypack.due_topics(conn) == ["d-chain"]
        second = studypack.push(conn, "d-chain")
        assert (second["part"], second["pages"]) == (2, 2)  # no try-first: page 3 + recall
        assert "(2/2)" in second["title"]
        sent = get_setting(conn, "rm_pack:d-chain")
        assert len(sent["docs"]) == 2  # both parts still map pack pages to book pages
        set_setting(conn, "rm_pack:d-chain", {**sent, "date": "2000-01-01"})
        conn.execute("INSERT INTO reading_pages(book, page, first_ts) VALUES('STW', 3, 'x')")
        assert studypack.due_topics(conn) == []  # all parts sent
