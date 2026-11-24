"""Interactive lessons: content integrity + engine/API behaviour."""

import yaml
import pytest

from calc_tutor import db, engine
from calc_tutor.db import connect

LESSONS = yaml.safe_load((db.CURRICULUM_DIR / "_lessons.yaml").read_text())["lessons"]
TOPIC_IDS = {t["id"] for u in db.load_curriculum() for t in u["topics"]}
WIDGETS = {"tiles", "complex", "zoom"}
ASKS = [(ls["id"], i, st["ask"]) for ls in LESSONS for i, st in enumerate(ls["steps"])
        if "ask" in st]


def test_lessons_reference_real_topics_and_widgets():
    ids = [ls["id"] for ls in LESSONS]
    assert len(ids) == len(set(ids))
    for ls in LESSONS:
        assert ls["topic"] in TOPIC_IDS, ls["id"]
        assert ls["steps"], ls["id"]
        for st in ls["steps"]:
            assert st.get("text") or st.get("widget"), ls["id"]
            if "widget" in st:
                assert st["widget"]["type"] in WIDGETS
            if "goal" in st:
                assert "widget" in st, f"{ls['id']}: a goal needs a widget"
            if st.get("ask", {}).get("predict"):
                assert "widget" in st, f"{ls['id']}: predict-first needs a widget to reveal"


@pytest.mark.parametrize("lid,i,ask", ASKS, ids=[f"{l}:{i}" for l, i, _ in ASKS])
def test_lesson_answers_grade_themselves(lid, i, ask):
    if ask["type"] == "mc":
        assert 0 <= int(ask["answer"]) < len(ask["choices"])
    r = engine.check_lesson_answer(ask["type"], str(ask["answer"]), str(ask["answer"]),
                                   ask.get("var"), ask.get("choices"))
    assert r["correct"] is True, r


def test_lesson_run_gives_xp_but_no_mastery():
    with connect() as conn:
        lid = LESSONS[0]["id"]
        topic = LESSONS[0]["topic"]
        assert any(x["id"] == lid for x in engine.get_topic(conn, topic)["lessons"])
        out = engine.complete_lesson(conn, lid, correct=5, total=6)
        assert out["first_time"] and out["award"]["xp"] == 25
        assert engine.complete_lesson(conn, lid, 5, 6)["award"]["xp"] == 15
        assert engine.consistency(conn)["studied_today"]
        ev = engine.mastery_evidence(conn, topic)
        assert ev["unaided_distinct_correct"]["value"] == 0
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_check_handles_complex_and_unparseable():
    assert engine.check_lesson_answer("num", "-1+2*I", "-1 + 2i")["correct"] is True
    assert engine.check_lesson_answer("num", "-1+2*I", "2i-1")["correct"] is True
    assert engine.check_lesson_answer("num", "9", "x;import os").get("retry")
