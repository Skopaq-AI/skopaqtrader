"""Tests for the TypeSafe Jev client (skopaq/llm/jev.py).

Requests go through the real ``typesafe-sdk`` client with an ``httpx2``
mock transport — no network, no API key.
"""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest

from skopaq.llm import jev as jev_module
from skopaq.llm.jev import (
    CATALYST_QUESTIONS,
    EXIT_QUESTION,
    TRADE_QUESTION,
    CatalystScore,
    Jev,
    JevVerdict,
)


def _answer(choice: str, probabilities: dict[str, float], confidence: float) -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {"answer": {
            "type": "choice", "choice": choice,
            "confidence": confidence, "probabilities": probabilities,
        }},
        "usage": {"input_tokens": 300, "output_tokens": 20},
    }


def _jev(handler, **kwargs) -> Jev:
    return Jev(api_key="test-key", transport=httpx2.MockTransport(handler), **kwargs)


class TestAsk:
    def test_trade_action_request_and_verdict(self):
        seen = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return httpx2.Response(200, json=_answer(
                "BUY", {"BUY": 0.82, "HOLD": 0.15, "SELL": 0.03}, 0.74))

        verdict = asyncio.run(_jev(handler).trade_action("**Rating**: Overweight"))

        assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
        assert seen["auth"] == "Bearer test-key"
        assert seen["body"]["model"] == "jev-1.13.0"  # pinned, not jev-latest
        assert seen["body"]["state"] == {"decision": "**Rating**: Overweight"}
        criteria = seen["body"]["questions"]["answer"]["criteria"]
        assert criteria.keys() == TRADE_QUESTION["criteria"].keys()
        assert verdict == JevVerdict(
            choice="BUY", confidence=0.74,
            probabilities={"BUY": 0.82, "HOLD": 0.15, "SELL": 0.03}, model="jev-1.13.0",
        )
        assert verdict.probability("SELL") == pytest.approx(0.03)

    def test_exit_action_uses_exit_question(self):
        seen = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen["body"] = json.loads(request.content)
            return httpx2.Response(200, json=_answer("HOLD", {"SELL": 0.3, "HOLD": 0.7}, 0.4))

        verdict = asyncio.run(_jev(handler).exit_action("RSI rolling over but trend intact"))

        assert seen["body"]["state"] == {"analysis": "RSI rolling over but trend intact"}
        criteria = seen["body"]["questions"]["answer"]["criteria"]
        assert set(criteria) == set(EXIT_QUESTION["criteria"])
        assert verdict.choice == "HOLD"

    def test_server_error_returns_none(self):
        calls = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(1)
            return httpx2.Response(529, json={"error": "overloaded"})

        jev = _jev(handler)
        jev._timeout = 0.5
        assert asyncio.run(jev.trade_action("**Rating**: Buy")) is None
        assert calls  # it did try

    def test_empty_text_skips_the_request(self):
        def handler(request):  # pragma: no cover - must not be called
            raise AssertionError("no request expected")

        assert asyncio.run(_jev(handler).trade_action("  ")) is None
        assert asyncio.run(_jev(handler).exit_action("")) is None

    def test_catalyst_asks_score_and_noul_in_one_request(self):
        seen = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(json.loads(request.content))
            return httpx2.Response(200, json={
                "model": "jev-1.13.0",
                "answers": {
                    "catalyst": {
                        "type": "score", "score": 2.3, "confidence": 0.7,
                        "legend": {str(i): c for i, c in enumerate(
                            CATALYST_QUESTIONS["catalyst"]["criteria"])},
                        "probabilities": {"0": 0.05, "1": 0.1, "2": 0.35, "3": 0.5},
                    },
                    "specific_news": {"type": "noul", "noul": 0.92},
                },
                "usage": {"input_tokens": 80, "output_tokens": 4},
            })

        score = asyncio.run(_jev(handler).catalyst("TCS", "Won a $2B order from a US bank"))

        assert len(seen) == 1
        assert seen[0]["state"] == {"stock": "TCS", "reason": "Won a $2B order from a US bank"}
        assert seen[0]["questions"]["catalyst"]["type"] == "score"
        assert seen[0]["questions"]["specific_news"]["type"] == "noul"
        assert score == CatalystScore(
            score=2.3, confidence=0.7, specific_news=0.92, model="jev-1.13.0")

    def test_catalyst_with_a_missing_answer_returns_none(self):
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json={
                "model": "jev-1.13.0",
                "answers": {"specific_news": {"type": "noul", "noul": 0.2}},
                "usage": {"input_tokens": 80, "output_tokens": 2},
            })

        assert asyncio.run(_jev(handler).catalyst("TCS", "Up 3%")) is None

    def test_describe_is_compact(self):
        verdict = JevVerdict("SELL", 0.9, {"SELL": 0.95, "HOLD": 0.05}, "jev-1.13.0")
        assert verdict.describe() == "Jev jev-1.13.0: SELL (confidence 0.90; HOLD=0.05, SELL=0.95)"


class TestGetJev:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        for var in ("SKOPAQ_JEV_ENABLED", "SKOPAQ_TYPESAFE_API_KEY", "SKOPAQ_JEV_MODEL",
                    "SKOPAQ_JEV_MIN_CONFIDENCE"):
            monkeypatch.delenv(var, raising=False)
        jev_module.get_jev.cache_clear()
        yield
        jev_module.get_jev.cache_clear()

    def test_off_by_default(self, monkeypatch):
        monkeypatch.setenv("SKOPAQ_TYPESAFE_API_KEY", "key")
        assert jev_module.get_jev() is None

    def test_enabled_without_key_is_off(self, monkeypatch):
        monkeypatch.setenv("SKOPAQ_JEV_ENABLED", "true")
        monkeypatch.setenv("SKOPAQ_TYPESAFE_API_KEY", "")
        assert jev_module.get_jev() is None

    def test_enabled_with_key(self, monkeypatch):
        monkeypatch.setenv("SKOPAQ_JEV_ENABLED", "true")
        monkeypatch.setenv("SKOPAQ_TYPESAFE_API_KEY", "key")
        monkeypatch.setenv("SKOPAQ_JEV_MIN_CONFIDENCE", "0.7")

        jev = jev_module.get_jev()

        assert isinstance(jev, Jev)
        assert jev.model == "jev-1.13.0"
        assert jev.min_confidence == 0.7
        assert jev.min_catalyst_score == 1.0
