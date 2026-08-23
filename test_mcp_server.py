from __future__ import annotations

import os

os.environ.setdefault("MCP_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1/db")
os.environ.setdefault("MCP_OWNER_TOKEN", "t" * 48)

from starlette.testclient import TestClient

from mcp_server import OwnerBearerMiddleware


async def _ok_app(_scope, _receive, send):
    await send(
        {
            "type": "http.response.start",
            "status": 204,
            "headers": [[b"cache-control", b"private"]],
        }
    )
    await send({"type": "http.response.body", "body": b"", "more_body": False})


def test_owner_bearer_guard_fails_closed():
    token = "owner-secret-" + "x" * 32
    client = TestClient(OwnerBearerMiddleware(_ok_app, token))

    missing = client.get("/mcp")
    wrong = client.get("/mcp", headers={"Authorization": "Bearer wrong"})
    owner = client.get("/mcp", headers={"Authorization": f"Bearer {token}"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.headers["cache-control"] == "no-store"
    assert owner.status_code == 204


def test_short_owner_token_is_rejected():
    try:
        OwnerBearerMiddleware(_ok_app, "short")
    except ValueError as error:
        assert "32" in str(error)
    else:
        raise AssertionError("short owner token must be rejected")


def test_public_oauth_cors_and_dashboard_surfaces_are_gone():
    source = open("mcp_server.py", encoding="utf-8").read()
    readme = open("README.md", encoding="utf-8").read()
    assert "GitHubProvider" not in source
    assert "CORSMiddleware" not in source
    assert '"/admin/' not in source
    assert "public account onboarding" in readme
