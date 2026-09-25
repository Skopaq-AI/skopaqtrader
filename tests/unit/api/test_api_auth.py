"""Optional bearer-token guard (SKOPAQ_API_TOKEN) and CORS origin parsing."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from skopaq.api import auth
from skopaq.api.auth import cors_origins


@pytest.fixture
def client(monkeypatch):
    """The real API app, with a stand-in Kite module (kiteconnect is a deploy extra)."""
    kite = types.ModuleType("skopaq.broker.kite_client")
    kite.get_access_token = lambda: "kite-tok"
    monkeypatch.setitem(sys.modules, "skopaq.broker.kite_client", kite)
    from skopaq.api.server import app

    return TestClient(app)


def _token(monkeypatch, value: str) -> None:
    # Patch the config read so a developer .env cannot leak in.
    monkeypatch.setattr(auth, "SkopaqConfig", lambda: SimpleNamespace(api_token=SecretStr(value)))


def test_open_without_a_configured_token(client, monkeypatch):
    _token(monkeypatch, "")
    response = client.get("/api/kite/token")
    assert response.status_code == 200
    assert response.json() == {"access_token": "kite-tok"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic s3cret"},
     {"Authorization": "s3cret"}],
)
def test_kite_token_needs_the_bearer_token(client, monkeypatch, headers):
    _token(monkeypatch, "s3cret")
    response = client.get("/api/kite/token", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_kite_token_with_the_bearer_token(client, monkeypatch):
    _token(monkeypatch, "s3cret")
    response = client.get("/api/kite/token", headers={"Authorization": "Bearer s3cret"})
    assert response.status_code == 200


def test_chat_tool_endpoint_is_guarded(client, monkeypatch):
    _token(monkeypatch, "s3cret")
    body = {"tool": "get_quote", "args": {"symbol": "TCS"}}
    assert client.post("/api/chat/tool", json=body).status_code == 401
    assert client.post(
        "/api/chat/tool", json=body, headers={"Authorization": "Bearer wrong"}
    ).status_code == 401

    def teapot(_session_id):
        raise HTTPException(418, "reached the endpoint")

    monkeypatch.setattr("skopaq.chat.bridge._get_or_create_session", teapot)
    response = client.post("/api/chat/tool", json=body,
                           headers={"Authorization": "bearer s3cret"})  # scheme is case-insensitive
    assert response.status_code == 418


def test_health_stays_open(client, monkeypatch):
    _token(monkeypatch, "s3cret")
    assert client.get("/health").status_code == 200


def test_cors_origins():
    def cfg(value):
        return SimpleNamespace(cors_origins=value)

    assert cors_origins(cfg("*")) == ["*"]
    assert cors_origins(cfg("https://a, https://b")) == ["https://a", "https://b"]
    assert cors_origins(cfg("")) == []
