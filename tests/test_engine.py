from datetime import date, datetime, timedelta

import pytest

from calc_tutor import config, db, engine, scheduler, wellness
from calc_tutor.db import connect, iso, utcnow


def qids(conn, topic, auto=True):
    sql = "SELECT id FROM questions WHERE topic_id=? AND active=1"
    if auto:
        sql += " AND type IN ('mc','num','expr','antideriv')"
    return [r[0] for r in conn.execute(sql + " ORDER BY id", (topic,))]


def key(conn, qid):
    return conn.execute("SELECT answer FROM questions WHERE id=?", (qid,)).fetchone()[0]


def ago(days):
    return iso(utcnow() - timedelta(days=days))


# --- basics -----------------------------------------------------------------------


def test_sync_is_idempotent():
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    db.init_db()
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == n


def test_levels():
    assert engine.level_info(0)["level"] == 1
    assert engine.level_info(100)["level"] == 2
    assert engine.level_info(10**6)["title"] == engine.LEVEL_TITLES[-1]


def test_migrates_old_database(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
      CREATE TABLE units(id TEXT PRIMARY KEY, ord INTEGER, title TEXT, constellation TEXT,
        color TEXT, blurb TEXT, ds_why TEXT);
      CREATE TABLE cards(id INTEGER PRIMARY KEY, key TEXT UNIQUE, topic_id TEXT,
        question_id INTEGER, front TEXT, back TEXT, ease REAL DEFAULT 2.5, interval REAL DEFAULT 0,
        reps INTEGER DEFAULT 0, lapses INTEGER DEFAULT 0, due TEXT, last_review TEXT,
        active INTEGER DEFAULT 0, created_at TEXT);
      INSERT INTO units VALUES('U00',0,'x','x','x','x','x');
      INSERT INTO cards(key, topic_id, front, back, interval, reps, due, last_review, active)
        VALUES('legacy','fn-basics','f','b',6,3,'2026-10-10T00:00:00+00:00',
               '2026-10-04T00:00:00+00:00',1);""")
    old.commit()
    old.close()
    monkeypatch.setenv("CALC_TUTOR_DB_PATH", str(path))
    db.init_db()
    with connect() as conn:
        assert conn.execute("SELECT course FROM units WHERE id='U00'").fetchone()[0] == "core"
        card = dict(conn.execute("SELECT * FROM cards WHERE key='legacy'").fetchone())
        assert 0 < engine.retrievability(conn, card) <= 1  # SM-2 history converts to FSRS
        out = engine.apply_review(conn, card["id"], engine.GOOD)
        assert out["interval_days"] > 1


# --- guardrails: hidden answers, retry, hint ladder -------------------------------


def test_wrong_answer_hides_solution_and_hints_need_an_attempt():
    with connect() as conn:
        q = qids(conn, "fn-basics")[0]
        with pytest.raises(ValueError, match="Attempt first"):
            engine.get_hint(conn, q)
        r = engine.record_answer(conn, q, "x+1")
        assert r["correct"] is False and "answer" not in r and r["can_retry"]
        h1 = engine.get_hint(conn, q)
        assert h1["level"] == 1 and h1["hint"]
        h2 = engine.get_hint(conn, q)
        assert h2["level"] == 2 and h2["hint"].startswith("Method")
        h4 = engine.get_hint(conn, q, level=4)
        assert h4["revealed"] and h4["answer"] == "9x^2+1"
        assert h4["followup_question"] and h4["followup_question"]["id"] != q
        assert "answer" not in h4["followup_question"]
        # a correct answer now is "with help": reduced XP, recorded as hinted
        r2 = engine.record_answer(conn, q, "9x^2+1")
        assert r2["correct"] and r2["hints_used"] == 3 and r2["award"]["xp"] < 10
        assert conn.execute("SELECT COUNT(*) FROM hint_events").fetchone()[0] == 3


def test_override_hint_is_recorded():
    with connect() as conn:
        q = qids(conn, "fn-basics")[0]
        engine.get_hint(conn, q, override=True)
        assert conn.execute("SELECT override FROM hint_events").fetchone()[0] == 1


def test_first_try_unaided_gets_full_xp_and_assistant_channel_is_aided():
    with connect() as conn:
        q1, q2 = qids(conn, "fn-basics")[:2]
        r = engine.record_answer(conn, q1, "9x^2+1")
        assert r["correct"] and r["award"]["xp"] == 10 and r["explanation"]
        r = engine.record_answer(conn, q2, "3x^2+3", channel="assistant")
        assert r["correct"] and r["award"]["xp"] < 10
        assert conn.execute("SELECT aided FROM attempts WHERE question_id=?", (q2,)
                            ).fetchone()[0] == 1


def test_free_response_shows_model_answer_only_after_an_attempt():
    with connect() as conn:
        free = conn.execute("SELECT id FROM questions WHERE type='free' AND source='seed' "
                            "LIMIT 1").fetchone()[0]
        pub = engine.public_question(conn, free)
        assert "answer" not in pub and "model_answer" not in pub
        r = engine.record_answer(conn, free, "because")
        assert r["needs_grading"] and r["model_answer"]
        assert engine.record_answer(conn, free, "because", correct=True)["correct"]


def test_unparseable_answer_is_not_recorded():
    with connect() as conn:
        r = engine.record_answer(conn, qids(conn, "fn-basics")[0], "x;import os")
        assert r["retry"]
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_pretest_shows_solution_and_leaves_cards_alone():
    with connect() as conn:
        qs = engine.pick_questions(conn, "d-chain", 5, "pretest")
        assert 1 <= len(qs) <= 2
        r = engine.record_answer(conn, qs[0]["id"], "0", channel="pretest")
        assert r["correct"] is False and r["answer"]  # pretest = feedback right away
        assert conn.execute("SELECT COUNT(*) FROM cards WHERE active=1").fetchone()[0] == 0


def test_self_explanation_saved():
    with connect() as conn:
        q = qids(conn, "fn-basics")[0]
        engine.record_answer(conn, q, "1")
        engine.save_self_explanation(conn, q, "apply g first, then f")
        assert conn.execute("SELECT note FROM attempts").fetchone()[0].startswith("apply")


# --- completion, FSRS, mastery gate -------------------------------------------------


def test_completion_is_provisional_and_cards_enter_fsrs_as_new():
    with connect() as conn:
        engine.start_topic(conn, "fn-explog")
        done = engine.complete_topic(conn, "fn-explog")
        assert done["provisional"] and done["award"]["xp"] == 30  # half of 60
        assert done["unlocked_facts"]
        due = engine.due_cards(conn, 50)
        assert due and all(c["topic_id"] == "fn-explog" for c in due)
        assert len(due) <= engine.DEFAULT_SETTINGS["new_cards_per_day"]
        concept = next(c for c in due if not c["question_id"])
        out = engine.review_card(conn, concept["id"], engine.GOOD)
        assert out["due"] > iso()
        assert engine.complete_topic(conn, "fn-explog")["already_completed"]


def test_new_card_daily_limit():
    with connect() as conn:
        db.set_setting(conn, "new_cards_per_day", 2)
        engine.complete_topic(conn, "fn-explog")
        first = engine.due_cards(conn, 50)
        assert len(first) == 2
        for c in first:
            engine.review_card(conn, c["id"], engine.GOOD)
        assert [c for c in engine.due_cards(conn, 50) if not c["last_review"]] == []


def test_review_problem_cards_show_a_fresh_variant():
    with connect() as conn:
        engine.complete_topic(conn, "d-chain")
        q0 = qids(conn, "d-chain")[0]
        card = conn.execute("SELECT * FROM cards WHERE question_id=?", (q0,)).fetchone()
        engine.apply_review(conn, card["id"], engine.GOOD)
        conn.execute("UPDATE cards SET due=? WHERE id=?", (ago(0.01), card["id"]))
        shown = next(c for c in engine.due_cards(conn, 50) if c["id"] == card["id"])
        assert shown["question"]["id"] != q0  # reviewed before -> a sibling problem


def test_mastery_needs_unaided_delayed_evidence():
    with connect() as conn:
        topic = "fn-explog"
        qs = qids(conn, topic)
        engine.complete_topic(conn, topic)
        for q in qs * 2:  # plenty of correct, unaided answers -- but all on the same day
            engine.record_answer(conn, q, key(conn, q))
        ev = engine.mastery_evidence(conn, topic)
        assert ev["unaided_distinct_correct"]["ok"] and not ev["delayed_or_interleaved_success"]
        assert not ev["mastered"]
        # pretend the lesson was completed 5 days ago; a correct answer now is delayed recall
        conn.execute("UPDATE progress SET completed_at=? WHERE topic_id=?", (ago(6), topic))
        conn.execute("UPDATE attempts SET ts=?", (ago(5),))  # those answers were days ago
        r = engine.record_answer(conn, qs[0], key(conn, qs[0]))
        assert "delayed recall" in r["award"]["reason"]
        assert r["mastery"] and r["mastery"]["award"]["xp"] == 60 // 2 + 25
        assert engine.mastery_evidence(conn, topic)["mastered"]
        assert engine.check_mastery(conn, topic) is None  # awarded once
        assert "first_star" in {a[0] for a in conn.execute("SELECT key FROM achievements")}
        status = {t["id"]: t["status"] for u in engine.curriculum(conn) for t in u["topics"]}
        assert status[topic] == "mastered"


def test_mastery_ignores_assistant_aided_attempts():
    with connect() as conn:
        topic = "fn-explog"
        engine.complete_topic(conn, topic)
        conn.execute("UPDATE progress SET completed_at=? WHERE topic_id=?", (ago(5), topic))
        for q in qids(conn, topic) * 3:
            engine.record_answer(conn, q, key(conn, q), channel="assistant")
        assert engine.mastery_evidence(conn, topic)["unaided_distinct_correct"]["value"] == 0


# --- mistakes -------------------------------------------------------------------------


def test_mistake_becomes_card_and_resolves_only_with_new_delayed_evidence():
    with connect() as conn:
        q = qids(conn, "d-chain")
        m = engine.log_mistake(conn, "d-chain", "forgot inner derivative",
                               "thinks chain rule = outer derivative only", question_id=q[0],
                               confident=True)
        card = conn.execute("SELECT * FROM cards WHERE id=?", (m["card_id"],)).fetchone()
        assert card["active"] == 1 and card["kind"] == "mistake" and card["priority"] == 1
        with pytest.raises(ValueError, match="Not yet"):
            engine.resolve_mistake(conn, m["mistake_id"])
        engine.record_answer(conn, q[1], key(conn, q[1]))  # same day: not enough
        assert conn.execute("SELECT resolved FROM mistakes").fetchone()[0] == 0
        conn.execute("UPDATE mistakes SET ts=?", (ago(2),))
        r = engine.record_answer(conn, q[2], key(conn, q[2]))
        assert r["mistakes_resolved"] == [m["mistake_id"]]
        assert conn.execute("SELECT active FROM cards WHERE id=?", (m["card_id"],)
                            ).fetchone()[0] == 0


def test_resolve_override_is_recorded():
    with connect() as conn:
        m = engine.log_mistake(conn, "d-chain", "x")
        out = engine.resolve_mistake(conn, m["mistake_id"], override=True)
        assert out["override"] and out["award"]["xp"] == 5


# --- consistency ------------------------------------------------------------------


def _study(conn, days_ago_list, protected=()):
    today = engine.local_today()
    for d in days_ago_list:
        conn.execute("INSERT OR REPLACE INTO study_days(local_date, studied) VALUES(?,1)",
                     ((today - timedelta(days=d)).isoformat(),))
    for d in protected:
        engine.protect_day(conn, (today - timedelta(days=d)).isoformat(), "rest")


def test_never_miss_twice_chain():
    with connect() as conn:
        _study(conn, [1, 3, 4, 5])           # missed only 2 days ago: chain survives
        assert engine.consistency(conn)["chain"] == 5
        conn.execute("DELETE FROM study_days")
        _study(conn, [1, 4, 5])              # missed 2 AND 3 days ago: chain restarts
        assert engine.consistency(conn)["chain"] == 1
        conn.execute("DELETE FROM study_days")
        _study(conn, [1, 4, 5], protected=[2])  # a protected day bridges the gap
        assert engine.consistency(conn)["chain"] == 5


def test_comeback_bonus_once_per_day():
    with connect() as conn:
        _study(conn, [3])
        out = engine.mark_studied(conn)  # yesterday missed, unprotected
        assert out and "comeback" in out["reason"]
        assert engine.mark_studied(conn) is None


def test_weekly_summary_text():
    with connect() as conn:
        engine.mark_studied(conn)
        assert "1/5 days" in engine.weekly_summary(conn)["text"]


# --- interleaving, blocks, health, verification -----------------------------------------


def test_mixed_quiz_interleaves_look_alike_topics():
    with connect() as conn:
        for t in ("int-sub", "int-parts"):
            engine.start_topic(conn, t)
        qs = engine.pick_questions(conn, mode="mixed", n=4)
        assert {q["topic_id"] for q in qs} == {"int-sub", "int-parts"}


def test_training_block():
    with connect() as conn:
        b = engine.block_info(conn)
        assert b["number"] == 1 and b["week"] == 1 and not b["test_ready"]
        db.set_setting(conn, "block_start",
                       (engine.local_today() - timedelta(days=41)).isoformat())
        assert engine.block_info(conn)["test_ready"]
        out = engine.complete_block(conn, 12, 15)
        assert out["score"] == 80.0 and engine.block_info(conn)["number"] == 2


def test_learning_health_numbers():
    with connect() as conn:
        engine.complete_topic(conn, "fn-explog")
        q = qids(conn, "fn-explog")
        engine.record_answer(conn, q[0], "0", confidence=1)  # confident miss
        engine.get_hint(conn, q[0])
        engine.record_answer(conn, q[1], key(conn, q[1]), channel="mixed", confidence=1)
        h = engine.learning_health(conn)
        assert h["calibration_gap"]["value"] == 0.5
        assert h["hints_per_problem"]["value"] == 0.5
        assert h["unaided_delayed_accuracy"]["n"] == 1


def test_add_question_requires_cas_verification():
    with connect() as conn:
        with pytest.raises(ValueError, match="verification required"):
            engine.add_question(conn, "d-chain", "expr", "Differentiate $e^{x^2}$", "2x e^(x^2)")
        with pytest.raises(ValueError, match="CAS disagrees"):
            engine.add_question(conn, "d-chain", "expr", "Differentiate $e^{x^2}$", "x e^(x^2)",
                                verify="diff(exp(x**2), x)")
        ok = engine.add_question(conn, "d-chain", "expr", "Differentiate $e^{x^2}$",
                                 "2x e^(x^2)", verify="diff(exp(x**2), x)")
        assert ok["verified"]
        assert engine.record_answer(conn, ok["question_id"], "2xe^(x^2)")["correct"]
        mc = engine.add_question(conn, "d-chain", "mc", "Pick", "1", choices=["a", "b"])
        assert mc["verified"] is False


# --- placement ----------------------------------------------------------------------


def test_diagnostic_flow():
    with connect() as conn:
        st = engine.diag_start(conn)
        run = st["run"]["id"]
        assert st["run"]["total"] > 50 and len(st["sections"]) == 13
        assert engine.diag_start(conn)["run"]["id"] == run
        with pytest.raises(ValueError):
            engine.record_answer(conn, st["sections"][0]["question_ids"][0], "1")
        keys = {r[0]: r[1] for r in conn.execute(
            "SELECT id, answer FROM questions WHERE source='diagnostic'")}
        for s in st["sections"]:
            for q in s["question_ids"]:
                if s["id"] in ("arith", "exponents"):
                    r = engine.diag_answer(conn, run, q, keys[q])
                    assert r["correct"] and r["explanation"] is not None
                elif s["id"] == "trig":
                    engine.diag_answer(conn, run, q, dont_know=True)
        res = engine.diag_finish(conn, run)
        levels = {r["id"]: r["level"] for r in res["results"]}
        assert levels["arith"] == "strong" and levels["trig"] == "learn"
        out = engine.diag_apply(conn, run)
        assert {t["id"] for t in out["placed"]} == {
            "pc-numbers", "pc-exponents",  # precalc bootcamp ...
            "al-integers", "al-fractions", "al-real-numbers", "al-exponents",  # ... and the
            "al-rational-exponents"}                                         # algebra course
        assert engine.next_topic(conn, "core")["id"] == "pc-polynomials"


# --- courses & pathway ------------------------------------------------------------------


def test_courses_and_pathway_order():
    with connect() as conn:
        cs = {c["id"]: c for c in engine.courses(conn)}
        assert list(cs) == ["algebra", "core", "neuro"]  # algebra is the on-ramp
        assert cs["algebra"]["next_topic"]["id"] == "al-integers"
        assert cs["core"]["next_topic"]["id"] == "pc-numbers"
        assert cs["neuro"]["next_topic"]["id"] == "nr-brain-behavior"
        order = [u["id"] for u in engine.curriculum(conn, "core")]
        assert order.index("U03") < order.index("L01") < order.index("L02") < order.index("U04")
        assert order.index("U04") < order.index("S01") < order.index("U05")
        assert order.index("L03") < order.index("U06") and order.index("U08") < order.index("S02")


def test_backlog_flag_blocks_new_topics():
    with connect() as conn:
        db.set_setting(conn, "backlog_limit", 0)
        engine.complete_topic(conn, "fn-explog")
        c = conn.execute("SELECT id FROM cards WHERE active=1 LIMIT 1").fetchone()[0]
        engine.apply_review(conn, c, engine.AGAIN)
        conn.execute("UPDATE cards SET due=? WHERE id=?", (ago(1), c))
        assert engine.next_topic(conn)["backlog"] is True


# --- scheduler & wellness -----------------------------------------------------------------


def _at(hour, minute=0, day=None):
    d = day or engine.local_today()
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=config.tz())


def _kinds(conn):
    return [r[0] for r in conn.execute("SELECT kind FROM notifications")]


def test_scheduler_sends_one_cue_only_if_not_studied():
    with connect() as conn:
        engine.start_topic(conn, "fn-basics")
        conn.execute("DELETE FROM study_days")
    scheduler.tick(_at(19, 30))
    scheduler.tick(_at(19, 31))
    with connect() as conn:
        assert _kinds(conn).count("nudge") == 1
        assert "popquiz" not in _kinds(conn)  # opt-in only
        conn.execute("DELETE FROM notifications")
        conn.execute("DELETE FROM settings WHERE key='cue_sent'")
        engine.mark_studied(conn)
    scheduler.tick(_at(19, 45))
    with connect() as conn:
        assert "nudge" not in _kinds(conn)


def test_scheduler_digest_only_when_something_is_due():
    tuesday = engine.local_today()
    while tuesday.weekday() != 1:
        tuesday += timedelta(days=1)
    scheduler.tick(_at(8, 0, tuesday))
    with connect() as conn:
        assert "digest" not in _kinds(conn)


def test_opt_in_pop_quiz_max_one():
    with connect() as conn:
        engine.start_topic(conn, "fn-basics")
        db.set_setting(conn, "pop_quizzes_per_day", 1)
        db.set_setting(conn, "pop_window", [0, 1])
    scheduler.tick(_at(23, 0))
    scheduler.tick(_at(23, 30))
    with connect() as conn:
        assert _kinds(conn).count("popquiz") == 1


def test_wellness_evaluation_and_protection():
    day = date(2026, 10, 19)
    data = {"sleep_hours": 7.5, "runs": [
        {"date": "2026-10-18", "km": 42.2, "race": True, "name": "City Marathon"},
        {"date": "2026-10-11", "km": 29.0, "race": False, "name": "Long run"}]}
    ev = wellness.evaluate(day, data)
    assert ev["recovery_day"] and any("raced" in r for r in ev["reasons"])
    short = wellness.evaluate(day, {"sleep_hours": 5.9, "runs": []})
    assert short["recovery_day"] and "short sleep" in short["reasons"][0]
    assert not wellness.evaluate(day, {"sleep_hours": 8, "runs": []})["recovery_day"]
    with connect() as conn:
        days = wellness.protect_from_runs(conn, data)
        assert set(days) == {"2026-10-18", "2026-10-19", "2026-10-11"}


def test_dashboard_shape():
    with connect() as conn:
        d = engine.dashboard(conn)
        for k in ("consistency", "learning_health", "block", "wellness", "backlog",
                  "new_cards_ready", "topics_mastered"):
            assert k in d
        assert d["topics_total"] > 150
