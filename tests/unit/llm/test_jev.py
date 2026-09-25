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


@pytest.fixture(autouse=True)
def _no_gateway_env(monkeypatch):
    """The SDK reads TYPESAFE_BASE_URL; keep a developer's value out of these tests."""
    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)


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

    def test_noul_returns_probability_and_model(self):
        seen = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen["body"] = json.loads(request.content)
            return httpx2.Response(200, json={
                "model": "jev-1.13.0",
                "answers": {"answer": {"type": "noul", "noul": 0.83}},
                "usage": {"input_tokens": 40, "output_tokens": 1},
            })

        result = asyncio.run(_jev(handler).noul({"text": "Board approves buyback"},
                                                "Does `text` announce a buyback?"))

        assert result == (0.83, "jev-1.13.0")
        assert seen["body"]["questions"]["answer"] == {
            "type": "noul", "instructions": "Does `text` announce a buyback?"}

    def test_describe_is_compact(self):
        verdict = JevVerdict("SELL", 0.9, {"SELL": 0.95, "HOLD": 0.05}, "jev-1.13.0")
        assert verdict.describe() == "Jev jev-1.13.0: SELL (confidence 0.90; HOLD=0.05, SELL=0.95)"


class TestGateway:
    """Jev served by a TypeSafe-compatible gateway (OpenRouter) instead of api.typesafe.ai."""

    def test_requests_go_to_the_base_url(self):
        seen = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["authorization"]
            seen["model"] = json.loads(request.content)["model"]
            return httpx2.Response(
                200, json=_answer("HOLD", {"BUY": 0.2, "HOLD": 0.7, "SELL": 0.1}, 0.5))

        jev = _jev(handler, base_url="https://openrouter.ai/api/", model="jev-1.13")
        verdict = asyncio.run(jev.trade_action("**Rating**: Hold"))

        assert seen == {"url": "https://openrouter.ai/api/v1/systemone",
                        "auth": "Bearer test-key", "model": "jev-1.13"}
        assert verdict.choice == "HOLD"
        assert jev.endpoint == "https://openrouter.ai/api"

    def test_without_a_base_url_the_sdk_variable_applies(self, monkeypatch):
        seen = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen["url"] = str(request.url)
            return httpx2.Response(200, json=_answer("BUY", {"BUY": 0.9, "HOLD": 0.1}, 0.8))

        monkeypatch.setenv("TYPESAFE_BASE_URL", "https://gateway.example/typesafe")
        jev = _jev(handler)
        asyncio.run(jev.trade_action("**Rating**: Buy"))

        assert seen["url"] == "https://gateway.example/typesafe/v1/systemone"
        assert jev.endpoint == "https://gateway.example/typesafe"

    def test_default_endpoint(self):
        assert Jev(api_key="k").endpoint == "https://api.typesafe.ai"

    def test_failure_is_recorded_and_cleared(self):
        status = {"code": 404}

        def handler(request: httpx2.Request) -> httpx2.Response:
            if status["code"] != 200:
                return httpx2.Response(status["code"], json={"error": "model not found"})
            return httpx2.Response(200, json=_answer("BUY", {"BUY": 0.9, "HOLD": 0.1}, 0.8))

        jev = _jev(handler, base_url="https://openrouter.ai/api")
        assert asyncio.run(jev.trade_action("**Rating**: Buy")) is None
        assert "404" in jev.last_error

        status["code"] = 200
        assert asyncio.run(jev.trade_action("**Rating**: Buy")).choice == "BUY"
        assert jev.last_error == ""


class TestGetJev:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)  # SkopaqConfig reads .env from the working directory
        for var in ("SKOPAQ_JEV_ENABLED", "SKOPAQ_TYPESAFE_API_KEY", "SKOPAQ_JEV_MODEL",
                    "SKOPAQ_JEV_MIN_CONFIDENCE", "SKOPAQ_JEV_BASE_URL"):
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
        assert jev.min_catalyst_score == 0.0  # rank only by default

    def test_default_endpoint_is_typesafe(self, monkeypatch):
        monkeypatch.setenv("SKOPAQ_JEV_ENABLED", "true")
        monkeypatch.setenv("SKOPAQ_TYPESAFE_API_KEY", "key")

        jev = jev_module.get_jev()

        assert jev.base_url is None
        assert jev.endpoint == "https://api.typesafe.ai"

    def test_base_url_from_config(self, monkeypatch, caplog):
        monkeypatch.setenv("SKOPAQ_JEV_ENABLED", "true")
        monkeypatch.setenv("SKOPAQ_TYPESAFE_API_KEY", "or-key")
        monkeypatch.setenv("SKOPAQ_JEV_BASE_URL", " https://openrouter.ai/api ")
        monkeypatch.setenv("SKOPAQ_JEV_MODEL", "jev-1.13")

        with caplog.at_level("INFO", logger="skopaq.llm.jev"):
            jev = jev_module.get_jev()

        assert jev.base_url == "https://openrouter.ai/api"
        assert jev.model == "jev-1.13"
        assert "endpoint=https://openrouter.ai/api" in caplog.text
        assert "patch number" not in caplog.text

    def test_openrouter_with_a_patch_version_pin_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("SKOPAQ_JEV_ENABLED", "true")
        monkeypatch.setenv("SKOPAQ_TYPESAFE_API_KEY", "or-key")
        monkeypatch.setenv("SKOPAQ_JEV_BASE_URL", "https://openrouter.ai/api")

        with caplog.at_level("WARNING", logger="skopaq.llm.jev"):
            jev = jev_module.get_jev()

        assert jev is not None  # still on; the warning says how to fix it
        assert "SKOPAQ_JEV_MODEL=jev-1.13" in caplog.text

    def test_typesafe_itself_keeps_the_patch_pin_quietly(self, monkeypatch, caplog):
        monkeypatch.setenv("SKOPAQ_JEV_ENABLED", "true")
        monkeypatch.setenv("SKOPAQ_TYPESAFE_API_KEY", "key")

        with caplog.at_level("WARNING", logger="skopaq.llm.jev"):
            assert jev_module.get_jev().model == "jev-1.13.0"
        assert "patch number" not in caplog.text


class TestUpstreamPostScreen:
    """tradingagents/agents/post_screen.py follows TYPESAFE_BASE_URL (UPSTREAM_CHANGES.md #7)."""

    def _post(self, monkeypatch):
        from tradingagents.agents import post_screen

        seen = {}

        class Response:
            status_code = 200
            headers: dict = {}

            @staticmethod
            def json():
                return {"model": "jev-latest", "answers": {"about": {"type": "noul", "noul": 0.9}}}

        def post(url, json, headers, timeout):
            seen.update(url=url, auth=headers["Authorization"], model=json["model"])
            return Response()

        monkeypatch.setattr(post_screen.requests, "post", post)
        monkeypatch.setenv("TYPESAFE_API_KEY", "or-key")
        monkeypatch.delenv("TYPESAFE_DEFAULT_MODEL", raising=False)
        post_screen.system_one({"post": "p"}, {"about": post_screen.QUESTIONS["about"]})
        return seen

    def test_default_is_typesafe(self, monkeypatch):
        assert self._post(monkeypatch)["url"] == "https://api.typesafe.ai/v1/systemone"

    @pytest.mark.parametrize("value", ["https://openrouter.ai/api", "https://openrouter.ai/api/",
                                       "  https://openrouter.ai/api  "])
    def test_gateway_from_env(self, monkeypatch, value):
        monkeypatch.setenv("TYPESAFE_BASE_URL", value)
        seen = self._post(monkeypatch)
        assert seen == {"url": "https://openrouter.ai/api/v1/systemone",
                        "auth": "Bearer or-key", "model": "jev-latest"}

    def test_blank_env_is_typesafe(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_BASE_URL", "   ")
        assert self._post(monkeypatch)["url"] == "https://api.typesafe.ai/v1/systemone"
