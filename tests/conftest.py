import pytest


@pytest.fixture(autouse=True)
def tmp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CALC_TUTOR_DB_PATH", str(tmp_path / "tutor.db"))
    monkeypatch.setenv("CALC_TUTOR_LOG_PATH", str(tmp_path / "auth.log"))
    monkeypatch.setenv("CALC_TUTOR_TOKEN", "test-token")
    monkeypatch.setenv("CALC_TUTOR_PUBLIC_URL", "https://calc.example.com")
    monkeypatch.delenv("CALC_TUTOR_NTFY_URL", raising=False)
    from calc_tutor import db
    db.init_db()
    yield
