import json

import pytest
from fastmcp import Client
from test_studypack import books  # noqa: F401  (fixture: 6-page Stewart, 2.5 = pages 2-3)

from calc_tutor import config, reading, vault
from calc_tutor.db import connect, set_setting
from calc_tutor.server import mcp


@pytest.fixture
def mirror(tmp_path, monkeypatch, books):  # noqa: F811
    monkeypatch.setenv("CALC_TUTOR_REMARKABLE_HOST", "remarkable")
    monkeypatch.setenv("CALC_TUTOR_REMARKABLE_DIR", str(tmp_path / "rm"))
    reading._page_count.cache_clear()
    d = config.remarkable_dir() / "xochitl"
    d.mkdir(parents=True)

    def open_at(doc: str, page_index: int, name: str = "Stewart Calculus.pdf",
                page_count: int = 6, opened: int = 1):
        (d / f"{doc}.metadata").write_text(json.dumps({
            "type": "DocumentType", "visibleName": name, "parent": "",
            "lastOpened": str(opened), "lastOpenedPage": page_index}))
        (d / f"{doc}.content").write_text(json.dumps({"fileType": "pdf",
                                                      "pageCount": page_count}))
    return open_at


def test_textbook_pages_count_and_finish_a_reading(mirror):
    with connect() as conn:
        mirror("book", 0)
        assert reading.tick(conn) == 0  # where it was left before we watched: not new reading
        mirror("book", 1, opened=2)     # page 2: first page of Stewart 2.5
        assert reading.tick(conn) == 1
        assert reading.tick(conn) == 0  # nothing moved
        p = reading.progress(conn, "d-chain")[0]
        assert (p["pages_read"], p["pages"], p["done"]) == (1, 2, None)
        mirror("book", 2, opened=3)     # page 3: end of the section
        reading.tick(conn)
        p = reading.progress(conn, "d-chain")[0]
        assert p["pages_read"] == 2 and p["done"]
        note = conn.execute("SELECT body FROM notifications WHERE title='📖 Reading done'"
                            ).fetchone()
        assert note and "The Chain Rule" in note[0]


def test_small_forward_jumps_count_pages_between(mirror):
    with connect() as conn:
        mirror("book", 0)
        reading.tick(conn)
        mirror("book", 0, opened=2)
        reading.tick(conn)  # page 1
        mirror("book", 2, opened=3)
        reading.tick(conn)  # jumped to page 3 within minutes: page 2 counts too
        assert reading.progress(conn, "d-chain")[0]["pages_read"] == 2


def test_unknown_pdf_is_ignored(mirror):
    with connect() as conn:
        mirror("other", 0, name="Some article.pdf", page_count=17)
        reading.tick(conn)
        mirror("other", 3, name="Some article.pdf", page_count=17, opened=2)
        assert reading.tick(conn) == 0


def test_study_pack_pages_map_to_the_book(mirror):
    with connect() as conn:
        set_setting(conn, "rm_pack:d-chain", {"doc": "pack", "layout": [
            {"book": "STW", "first": 2, "pages": [2, 3]}]})
        mirror("pack", 0, name="Oct 20 · The Chain Rule", page_count=4)  # try-first page
        assert reading.tick(conn) == 0
        mirror("pack", 2, name="Oct 20 · The Chain Rule", page_count=4, opened=2)  # pack p3
        assert reading.tick(conn) == 1
        assert conn.execute("SELECT book, page FROM reading_log").fetchone()[:] == ("STW", 3)


async def test_recall_tool_and_vault(mirror, tmp_path):
    async with Client(mcp) as c:
        out = (await c.call_tool("save_recall", {
            "topic_id": "d-chain", "text": "Outside derivative times inside derivative.",
            "covered": ["outer times inner"], "missed": ["Leibniz form dy/dx = dy/du du/dx"]}
        )).data
        assert out["recall_id"]
        prog = (await c.call_tool("get_reading_progress", {"topic_id": "d-chain"})).data
        assert prog["readings"][0]["pages"] == 2
    with connect() as conn:
        mirror("book", 0)
        reading.tick(conn)
        mirror("book", 1, opened=2)
        reading.tick(conn)
        vault.export(conn, tmp_path / "vault")
    topic = next((tmp_path / "vault").glob("Topics/*/The Chain Rule.md")).read_text()
    assert "> Outside derivative times inside derivative." in topic
    assert "Missed: Leibniz form" in topic and "1/2 pages" in topic
    log = next((tmp_path / "vault").glob("Log/*.md")).read_text()
    assert "🧠 recall" in log and "new pages" in log
