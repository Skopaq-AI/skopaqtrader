"""Tests for the FastAPI chat bridge endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from skopaq.api.auth import require_api_token
from skopaq.chat.bridge import _sessions, router


@pytest.fixture
def client():
    """FastAPI test client with the chat bridge router."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    # A developer .env with SKOPAQ_API_TOKEN must not fail these tests (tests/unit/api covers auth).
    app.dependency_overrides[require_api_token] = lambda: None
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_sessions():
    """Clear session store between tests."""
    _sessions.clear()
    yield
    _sessions.clear()


@patch("skopaq.chat.bridge._get_or_create_session")
def test_send_message_returns_response(mock_session, client):
    session = MagicMock()
    session.id = "test-session-123"
    session.get_history.return_value = []
    session.messages = []

    mock_agent = MagicMock()
    ai_msg = MagicMock()
    ai_msg.type = "ai"
    ai_msg.content = "I can help with that!"
    mock_agent.ainvoke = AsyncMock(return_value={"messages": [ai_msg]})
    session.ensure_agent.return_value = mock_agent

    mock_session.return_value = session

    response = client.post(
        "/api/chat/message",
        json={"message": "hello", "session_id": ""},
    )
    assert response.status_code == 200
    data = response.json()
    assert "I can help" in data["message"]
    assert data["session_id"] == "test-session-123"


def test_send_message_empty_body(client):
    response = client.post(
        "/api/chat/message",
        json={"message": "", "session_id": ""},
    )
    assert response.status_code == 422  # Validation error (min_length=1)


@patch("skopaq.chat.bridge._get_or_create_session")
def test_tool_call_endpoint(mock_session, client):
    session = MagicMock()
    session.id = "test-session-456"
    session.ensure_infra.return_value = MagicMock()
    mock_session.return_value = session

    with patch("skopaq.chat.tools.get_all_tools") as mock_tools, \
         patch("skopaq.chat.tools.init_tools"):
        mock_tool = MagicMock()
        mock_tool.name = "get_portfolio"
        mock_tool.ainvoke = AsyncMock(return_value="Portfolio: ₹1,00,000")
        mock_tools.return_value = [mock_tool]

        response = client.post(
            "/api/chat/tool",
            json={"tool": "get_portfolio", "args": {}, "session_id": ""},
        )
        assert response.status_code == 200
        data = response.json()
        assert "Portfolio" in data["result"]


@patch("skopaq.chat.bridge._get_or_create_session")
def test_tool_call_unknown_tool(mock_session, client):
    session = MagicMock()
    session.id = "test-session-789"
    session.ensure_infra.return_value = MagicMock()
    mock_session.return_value = session

    with patch("skopaq.chat.tools.get_all_tools") as mock_tools, \
         patch("skopaq.chat.tools.init_tools"):
        mock_tool = MagicMock()
        mock_tool.name = "get_portfolio"
        mock_tools.return_value = [mock_tool]

        response = client.post(
            "/api/chat/tool",
            json={"tool": "nonexistent_tool", "args": {}, "session_id": ""},
        )
        assert response.status_code == 400
        assert "Unknown tool" in response.json()["detail"]
