from calc_tutor import engine, vault
from calc_tutor.db import connect


def test_export_writes_linked_notes_and_is_idempotent(tmp_path):
    root = tmp_path / "vault"
    with connect() as conn:
        engine.start_topic(conn, "d-chain")
        engine.log_mistake(conn, "d-chain", "forgot the inner derivative",
                           misconception="chain rule = differentiate the outside only",
                           confident=True)
        engine.record_grade(conn, "d-chain", 80)
        engine.add_journal(conn, "Chain rule intro", topics=["d-chain"], next_steps="practice")
        first = vault.export(conn, root)
        again = vault.export(conn, root)
    assert first["changed"] == first["notes"] and again["changed"] == 0

    topic = next(root.glob("Topics/*/The Chain Rule.md")).read_text()
    assert 'status: "learning"' in topic and "## Builds on" in topic and "[[" in topic
    assert "forgot the inner derivative" in topic and "80%" in topic
    assert "Chain rule intro" in topic

    mistakes = (root / "Mistakes.md").read_text()
    assert "[[The Chain Rule]]" in mistakes and "(I was sure)" in mistakes
    home = (root / "Home.md").read_text()
    assert "[[The Chain Rule]]" in home  # in progress
    log = next(root.glob("Log/*.md")).read_text()
    assert "Chain rule intro" in log and "📝 quiz" in log
    assert (root / "Courses" / "Math for Data Science.md").exists()


def test_export_removes_stale_notes(tmp_path):
    root = tmp_path / "vault"
    stray = root / "Topics" / "Old.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("gone soon")
    with connect() as conn:
        vault.export(conn, root)
    assert not stray.exists()


def test_note_names_are_obsidian_safe():
    assert vault._name("Polynomials: Expanding & Factoring") == "Polynomials – Expanding & Factoring"
    assert vault._name('a/b?c#d[e]') == "a-b-c-d-e"
