"""Curriculum integrity: every seed question must be self-consistent and gradeable."""

import json
import re

import pytest

from calc_tutor import db, grading

UNITS = db.load_curriculum()
BOOKS = db.load_books()
TOPICS = [t for u in UNITS for t in u["topics"]]
IDS = {t["id"] for t in TOPICS}
QUESTIONS = [(t["id"], i, q) for t in TOPICS for i, q in enumerate(t.get("questions", []))]


def test_unique_topic_ids():
    assert len(IDS) == len(TOPICS)


def test_prereqs_exist_and_acyclic():
    graph = {t["id"]: t.get("prereqs", []) for t in TOPICS}
    for tid, pre in graph.items():
        for p in pre:
            assert p in IDS, f"{tid} has unknown prereq {p}"
    state: dict[str, int] = {}

    def visit(n):
        assert state.get(n) != 1, f"cycle through {n}"
        if state.get(n) == 2:
            return
        state[n] = 1
        for p in graph[n]:
            visit(p)
        state[n] = 2

    for n in graph:
        visit(n)


def test_readings_reference_known_books():
    for t in TOPICS:
        for r in t.get("readings", []) + t.get("videos", []):
            assert r["book"] in BOOKS, f"{t['id']}: unknown book {r['book']}"


def test_every_topic_has_content():
    for t in TOPICS:
        assert t["summary"].strip() and t.get("key_ideas"), t["id"]
        assert t.get("questions"), f"{t['id']} has no questions"
        assert t.get("fun"), f"{t['id']} has no fun fact"
        assert t.get("readings"), f"{t['id']} has no readings"


@pytest.mark.parametrize("tid,i,q", QUESTIONS, ids=[f"{t}:{i}" for t, i, _ in QUESTIONS])
def test_answer_key_grades_itself(tid, i, q):
    row = {"type": q["type"], "answer": str(q["a"]), "var": q.get("var"), "tol": q.get("tol"),
           "choices": json.dumps(q.get("choices")) if "choices" in q else None}
    if q["type"] == "mc":
        assert 0 <= int(q["a"]) < len(q["choices"])
    if q["type"] == "free":
        return
    res = grading.check(row, str(q["a"]))
    assert res["correct"] is True, res


def test_no_cross_course_prereqs():
    course_of = {t["id"]: u["unit"].get("course", "core") for u in UNITS for t in u["topics"]}
    for t in TOPICS:
        for p in t.get("prereqs", []):
            assert course_of[p] == course_of[t["id"]], f"{t['id']} -> {p} crosses courses"


def test_units_reference_known_courses():
    courses = db.load_courses()
    for u in UNITS:
        assert u["unit"].get("course", "core") in courses, u["unit"]["id"]


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def test_no_leftover_yaml_quote_escapes():
    # '' is only an escape inside single-quoted scalars; in block scalars it leaks through
    # (f'' for a second derivative is legitimate, so only flag possessives like "app''s")
    for u in UNITS:
        for s in _strings(u):
            assert not re.search(r"[A-Za-z]''s\b", s), s[:80]


DIAG = __import__("yaml").safe_load((db.CURRICULUM_DIR / "_diagnostic.yaml").read_text())
DIAG_QS = [(s["id"], i, q) for s in DIAG["sections"] for i, q in enumerate(s["questions"])]


def test_diagnostic_sections_map_to_real_topics():
    for s in DIAG["sections"]:
        assert s["topics"] and all(t in IDS for t in s["topics"]), s["id"]


@pytest.mark.parametrize("sid,i,q", DIAG_QS, ids=[f"diag:{s}:{i}" for s, i, _ in DIAG_QS])
def test_diagnostic_answer_keys(sid, i, q):
    test_answer_key_grades_itself(sid, i, q)


def test_required_topics_never_depend_on_optional_ones():
    optional = {t["id"] for t in TOPICS if t.get("optional")}
    for t in TOPICS:
        if not t.get("optional"):
            bad = [p for p in t.get("prereqs", []) if p in optional]
            assert not bad, f"required {t['id']} depends on optional {bad}"


def test_interleave_groups_reference_real_topics():
    groups = __import__("yaml").safe_load((db.CURRICULUM_DIR / "_interleave.yaml").read_text())
    for g in groups["groups"]:
        assert len(g) >= 2 and all(t in IDS for t in g), g


def test_no_stray_dollar_amounts():
    # "$25" etc. would be swallowed by KaTeX's $...$ math delimiters
    for u in UNITS:
        for s in _strings(u):
            assert not re.search(r"\$\d[\d,.]*\s+(million|billion)\b|\$\d[\d,.]*[MK]\b", s), \
                s[:80]
