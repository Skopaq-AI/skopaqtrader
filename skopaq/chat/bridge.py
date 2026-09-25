"""FastAPI HTTP bridge for OpenClaw and external messaging channels.

Exposes the chat agent as REST endpoints so OpenClaw (or any HTTP client)
can send messages and receive responses.  Sessions are tracked in-memory
with a configurable TTL.

Mount this router on the main FastAPI app::

    from skopaq.chat.bridge import router as chat_router
    app.include_router(chat_router)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from skopaq.api.auth import require_api_token
from skopaq.chat.session import ChatSession

logger = logging.getLogger(__name__)

# The endpoints run tools (including trade_stock): SKOPAQ_API_TOKEN, when set, guards them.
router = APIRouter(
    prefix="/api/chat", tags=["chat"], dependencies=[Depends(require_api_token)],
)

# In-memory session store (session_id → ChatSession)
_sessions: dict[str, ChatSession] = {}
_SESSION_TTL_SECONDS = 3600  # 1 hour


# ── Request / Response Models ────────────────────────────────────────────────


class ChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User message text.")
    session_id: str = Field(
        default="",
        description="Session ID for conversation continuity. Empty = new session.",
    )


class ChatMessageResponse(BaseModel):
    message: str = Field(..., description="AI response text.")
    session_id: str = Field(..., description="Session ID for follow-up messages.")
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Tools that were called during this response.",
    )


class ToolCallRequest(BaseModel):
    tool: str = Field(..., description="Tool name (e.g., 'get_quote').")
    args: dict[str, Any] = Field(
        default_factory=dict, description="Tool arguments."
    )
    session_id: str = Field(default="", description="Optional session ID.")


class ToolCallResponse(BaseModel):
    result: str = Field(..., description="Tool execution result.")
    tool: str = Field(..., description="Tool that was called.")
    session_id: str = Field(..., description="Session ID.")


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/message", response_model=ChatMessageResponse)
async def send_message(body: ChatMessageRequest) -> ChatMessageResponse:
    """Send a message to the chatbot and get a response.

    This is the primary endpoint for OpenClaw integration.
    """
    _cleanup_expired_sessions()

    session = _get_or_create_session(body.session_id)

    try:
        from langchain_core.messages import HumanMessage

        session.add_user_message(body.message)
        agent = session.ensure_agent()
        config = session.thread_config

        # Run agent with auto-resume loop for interrupts.
        # The agent uses interrupt_before=["tools"] for the interactive REPL,
        # but the HTTP bridge has no human to confirm — auto-approve all tools.
        result = await agent.ainvoke(
            {"messages": session.get_history()},
            config=config,
        )

        # Auto-resume loop: if the agent interrupted before a tool call,
        # resume it (approve the tool) until it finishes.
        max_resumes = 10  # Safety limit
        for _ in range(max_resumes):
            state = agent.get_state(config)
            if not state.next:
                break  # Agent finished
            # Resume from interrupt (auto-approve tool call)
            result = await agent.ainvoke(None, config=config)

        # Extract final AI message and tool calls
        messages = result.get("messages", [])
        ai_text = ""
        tool_calls: list[dict[str, Any]] = []

        for msg in reversed(messages):
            if hasattr(msg, "type"):
                if msg.type == "ai" and msg.content and not ai_text:
                    ai_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                elif msg.type == "tool":
                    tool_calls.append({
                        "tool": getattr(msg, "name", "unknown"),
                        "result": msg.content[:500] if msg.content else "",
                    })

        if ai_text:
            session.add_ai_message(ai_text)

        return ChatMessageResponse(
            message=ai_text or "(No response generated)",
            session_id=session.id,
            tool_calls=tool_calls,
        )

    except Exception as exc:
        logger.exception("Chat message handler failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/tool", response_model=ToolCallResponse)
async def call_tool(body: ToolCallRequest) -> ToolCallResponse:
    """Call a specific trading tool directly.

    Useful for OpenClaw skill shortcuts that bypass the LLM.
    """
    _cleanup_expired_sessions()

    session = _get_or_create_session(body.session_id)

    try:
        from skopaq.chat.tools import get_all_tools, init_tools

        infra = session.ensure_infra()
        init_tools(infra)

        # Find the requested tool
        tools = get_all_tools()
        tool_fn = None
        for t in tools:
            if t.name == body.tool:
                tool_fn = t
                break

        if tool_fn is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown tool: {body.tool}. "
                f"Available: {[t.name for t in tools]}",
            )

        result = await tool_fn.ainvoke(body.args)
        return ToolCallResponse(
            result=str(result),
            tool=body.tool,
            session_id=session.id,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Tool call handler failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Session Management ───────────────────────────────────────────────────────


def _get_or_create_session(session_id: str) -> ChatSession:
    """Get an existing session or create a new one.

    If ``session_id`` is provided, the new session is stored under that
    key so subsequent calls with the same ID find it.  If empty, the
    session's auto-generated UUID is used as the key.
    """
    if session_id and session_id in _sessions:
        return _sessions[session_id]

    from skopaq.config import SkopaqConfig

    config = SkopaqConfig()
    session = ChatSession(config)

    # Store under the caller's ID if provided, otherwise use the session's own ID
    key = session_id if session_id else session.id
    _sessions[key] = session
    # Also update session.id to match the key for consistent responses
    if session_id:
        session.id = session_id
    return session


def _cleanup_expired_sessions() -> None:
    """Remove sessions older than TTL."""
    now = time.time()
    expired = [
        sid
        for sid, s in _sessions.items()
        if (now - s.created_at.timestamp()) > _SESSION_TTL_SECONDS
    ]
    for sid in expired:
        del _sessions[sid]
    if expired:
        logger.info("Cleaned up %d expired chat sessions", len(expired))
