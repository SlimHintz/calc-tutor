"""Minimal OAuth 2.1 + PKCE wrapper (same design as kitchen-inventory-mcp) so the
Remote MCP custom-connector flow (web, mobile, Desktop GUI) has a handshake to
perform. /oauth/authorize checks the shared CALC_TUTOR_TOKEN and, on a match,
issues an access token to whichever client dynamically registered.

Out of scope: refresh tokens (access tokens last 30 days, then the client
re-runs authorize) and a real consent screen.
"""

from __future__ import annotations

import sqlite3

import base64
import hashlib
import hmac
import html
import json
import secrets
from datetime import timedelta

from .db import iso, parse_iso, utcnow

AUTH_CODE_TTL = timedelta(minutes=10)
ACCESS_TOKEN_TTL = timedelta(days=30)


def verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method != "S256" or not code_verifier:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, code_challenge)


def register_client(conn: sqlite3.Connection, client_name: str | None, redirect_uris: list[str]) -> dict:
    client_id = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO oauth_clients(id, client_name, redirect_uris) VALUES(?,?,?)",
                 (client_id, client_name, json.dumps(redirect_uris)))
    return {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }


def get_client(conn: sqlite3.Connection, client_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM oauth_clients WHERE id=?", (client_id,)).fetchone()
    if not r:
        return None
    return {"id": r["id"], "client_name": r["client_name"],
            "redirect_uris": json.loads(r["redirect_uris"])}


def issue_authorization_code(conn: sqlite3.Connection, client_id, redirect_uri, code_challenge, method) -> str:
    code = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO oauth_codes(code, client_id, redirect_uri, code_challenge, "
        "code_challenge_method, expires_at) VALUES(?,?,?,?,?,?)",
        (code, client_id, redirect_uri, code_challenge, method, iso(utcnow() + AUTH_CODE_TTL)))
    return code


def exchange_code_for_token(conn: sqlite3.Connection, code, client_id, redirect_uri, code_verifier):
    """Returns (access_token, error_code)."""
    row = conn.execute("SELECT * FROM oauth_codes WHERE code=?", (code,)).fetchone()
    if row is None or row["used"] or parse_iso(row["expires_at"]) < utcnow():
        return None, "invalid_grant"
    if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
        return None, "invalid_grant"
    if not verify_pkce(code_verifier, row["code_challenge"], row["code_challenge_method"]):
        return None, "invalid_grant"
    conn.execute("UPDATE oauth_codes SET used=1 WHERE code=?", (code,))
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO oauth_tokens(token, client_id, expires_at) VALUES(?,?,?)",
                 (token, client_id, iso(utcnow() + ACCESS_TOKEN_TTL)))
    return token, None


def is_valid_access_token(conn: sqlite3.Connection, token: str) -> bool:
    row = conn.execute("SELECT expires_at FROM oauth_tokens WHERE token=?", (token,)).fetchone()
    return row is not None and parse_iso(row["expires_at"]) > utcnow()


def render_authorize_form(*, client_name, redirect_uri, hidden_fields, error) -> str:
    esc = html.escape
    hidden_html = "\n".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
                            for k, v in hidden_fields.items())
    error_html = f'<p class="error">{esc(error)}</p>' if error else ""
    return f"""<!doctype html>
<html><head><title>Calculus Tutor - Authorize</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #0b1020; color: #e8ecf8;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #141b33; padding: 2rem; border-radius: 14px; max-width: 360px; width: 90%;
          border: 1px solid #263055; }}
  h1 {{ font-size: 1.1rem; margin-top: 0; }}
  p.sub {{ color: #9aa6c8; font-size: 0.85rem; word-break: break-all; }}
  input[type=password] {{ width: 100%; padding: 0.6rem; border-radius: 8px; border: 1px solid #344070;
     background: #0b1020; color: #e8ecf8; box-sizing: border-box; margin-top: 0.5rem; }}
  button {{ width: 100%; padding: 0.7rem; margin-top: 1rem; border-radius: 8px; border: none;
            background: #f5b942; color: #1a1300; font-weight: 600; font-size: 1rem; }}
  .error {{ color: #ff7a7a; font-size: 0.9rem; }}
</style></head>
<body><div class="card">
  <h1>∫ {esc(client_name or "An application")} wants to connect to your Calculus Tutor</h1>
  <p class="sub">Redirecting to: {esc(redirect_uri)}</p>
  {error_html}
  <form method="post">{hidden_html}
    <label for="token">Tutor access token</label>
    <input type="password" name="token" id="token" autofocus required>
    <button type="submit">Authorize</button>
  </form>
</div></body></html>"""
