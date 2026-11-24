import base64
import hashlib
import json

import pytest
from fastmcp import Client
from starlette.testclient import TestClient

from calc_tutor.server import build_app, mcp

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client():
    with TestClient(build_app()) as c:
        yield c


def test_public_and_protected_routes(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/").status_code == 200
    r = client.get("/api/dashboard")
    assert r.status_code == 401
    assert "resource_metadata" in r.headers["www-authenticate"]
    assert client.get("/api/dashboard", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/dashboard", headers=AUTH).json()["level"]["level"] == 1


def test_api_learning_flow(client):
    t = client.get("/api/topic/fn-basics", headers=AUTH).json()
    assert "answer" not in t["questions"][0]
    qid = t["questions"][0]["id"]
    r = client.post("/api/answer", headers=AUTH,
                    json={"question_id": qid, "answer": "9x^2 + 1"}).json()
    assert r["correct"]
    assert client.post("/api/topic/fn-basics/complete", headers=AUTH).json()["award"]["xp"] == 20
    assert client.get("/api/facts", headers=AUTH).json()
    assert client.get("/api/topic/nope", headers=AUTH).status_code == 400
    s = client.post("/api/settings", headers=AUTH, json={"weekly_target": 4,
                                                          "study_time": "06:30"}).json()
    assert s["weekly_target"] == 4 and s["study_time"] == "06:30"
    wrong = client.post("/api/answer", headers=AUTH,
                        json={"question_id": qid, "answer": "x", "confidence": 1}).json()
    assert wrong["correct"] is False and "answer" not in wrong
    hint = client.post("/api/hint", headers=AUTH, json={"question_id": qid}).json()
    assert hint["level"] == 1
    assert client.post("/api/protect", headers=AUTH, json={}).json()["protected"]
    assert "text" in client.get("/api/weekly_summary", headers=AUTH).json()
    assert client.get("/api/block", headers=AUTH).json()["number"] == 1
    ls = client.get("/api/lessons", headers=AUTH).json()
    assert ls and client.get(f"/api/lesson/{ls[0]['id']}", headers=AUTH).json()["steps"]
    chk = client.post("/api/lesson/check", headers=AUTH,
                      json={"type": "num", "answer": "9", "given": "3^2"}).json()
    assert chk["correct"] is True
    done = client.post(f"/api/lesson/{ls[0]['id']}/complete", headers=AUTH,
                       json={"correct": 3, "total": 4}).json()
    assert done["award"]["xp"] > 0
    assert client.get("/static/lessons.js").status_code == 200


def test_static_path_traversal(client):
    assert client.get("/static/..%2Fserver.py").status_code == 404


def _pkce():
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()
                                         ).rstrip(b"=").decode()
    return verifier, challenge


def test_oauth_handshake(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["token_endpoint"] == "https://calc.example.com/oauth/token"
    reg = client.post("/oauth/register", json={"client_name": "Test Client",
                                               "redirect_uris": ["https://client.example/cb"]}).json()
    verifier, challenge = _pkce()
    form = {"client_id": reg["client_id"], "redirect_uri": "https://client.example/cb",
            "state": "s1", "code_challenge": challenge, "code_challenge_method": "S256"}
    assert client.get("/oauth/authorize", params=form).status_code == 200
    assert client.post("/oauth/authorize", data={**form, "token": "wrong"}).status_code == 401
    r = client.post("/oauth/authorize", data={**form, "token": "test-token"},
                    follow_redirects=False)
    assert r.status_code == 302
    code = r.headers["location"].split("code=")[1].split("&")[0]
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
        "redirect_uri": "https://client.example/cb", "code_verifier": verifier}).json()
    assert client.get("/api/dashboard",
                      headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 200
    # code is single use
    again = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
        "redirect_uri": "https://client.example/cb", "code_verifier": verifier})
    assert again.status_code == 400


async def test_mcp_tools_in_memory():
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
        assert {"get_student_dashboard", "get_topic", "check_answer", "get_hint",
                "reveal_solution", "check_step", "compute", "complete_topic", "log_mistake",
                "get_due_reviews", "save_session_notes", "get_learning_health", "protect_day",
                "get_weekly_summary"} <= names
        topic = (await c.call_tool("get_topic", {"topic_id": "d-chain"})).data
        q = topic["questions"][0]
        assert "answer" not in q  # guardrail: the tutor sees no answers up front
        bad = (await c.call_tool("check_answer", {"question_id": q["id"], "answer": "0",
                                                  "confidence": "sure"})).data
        assert bad["correct"] is False and "answer" not in bad
        hint = (await c.call_tool("get_hint", {"question_id": q["id"]})).data
        assert hint["level"] == 1
        sol = (await c.call_tool("reveal_solution", {"question_id": q["id"]})).data
        assert sol["answer"] == "30x(3x^2+1)^4"
        no_attempt = (await c.call_tool("get_hint", {"question_id": topic["questions"][1]["id"]}
                                        )).data
        assert "error" in no_attempt and "Attempt first" in no_attempt["error"]
        step = (await c.call_tool("check_step", {"before": "(x+1)^2", "after": "x^2+2x+1"})).data
        assert step["equivalent"] is True
        comp = (await c.call_tool("compute", {"expression": "diff(x**3, x)"})).data
        assert comp["result"] == "3*x**2"
        added = (await c.call_tool("add_question", {
            "topic_id": "d-chain", "type": "expr", "prompt": "Differentiate $\\sin(2x)$",
            "answer": "2cos(2x)", "verify": "diff(sin(2*x), x)"})).data
        assert added["verified"]
        unverified = (await c.call_tool("add_question", {
            "topic_id": "d-chain", "type": "num", "prompt": "1+1", "answer": "2"})).data
        assert "error" in unverified
        quiz = (await c.call_tool("get_quiz", {"topic_id": "d-chain", "n": 2})).structured_content
        assert all("answer" not in item for item in quiz["result"])
        cs = (await c.call_tool("get_courses", {})).structured_content["result"]
        assert {x["id"] for x in cs} == {"algebra", "core", "neuro"}
        assert all(x["tutor_notes"] for x in cs)
        notes = (await c.call_tool("save_session_notes", {"summary": "chain rule",
                                                          "next_steps": "implicit diff"})).data
        assert notes["journal_id"]
        dash = (await c.call_tool("get_student_dashboard", {})).data
        assert dash["last_session"]["summary"] == "chain rule"
        assert "learning_health" in dash and "consistency" in dash
        assert json.dumps(dash)
