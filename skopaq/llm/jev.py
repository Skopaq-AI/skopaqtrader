"""TypeSafe Jev — calibrated decisions on the text our LLM agents write.

Jev is a "System One" model: it answers typed questions about a text with
probabilities instead of generating text (about 0.1 s, $0.042 per million
input tokens, output free). Skopaq uses it where it used to regex an LLM's
prose for a decision:

- the Portfolio Manager's final decision → BUY / HOLD / SELL probabilities;
  the probability of the signal's own action becomes its confidence, and a
  confident contradiction downgrades the trade to HOLD;
- the sell analyst's analysis → SELL / HOLD, acted on only when confident;
- a scanner candidate's reason → catalyst strength (0-3), used to drop weak
  candidates and rank the rest before the slow full analysis.

Off unless ``SKOPAQ_JEV_ENABLED=true`` and ``SKOPAQ_TYPESAFE_API_KEY`` is set.
Every failure returns ``None`` and callers keep their previous behavior.

Jev is weak at arithmetic and dates, so only the semantic judgment is asked
of it; thresholds and P&L rules stay in code. See docs.typesafe.ai.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Answer keys double as Skopaq's order actions.
TRADE_QUESTION = {
    "type": "choice",
    "instructions": "Which trade does `decision` commit to for the stock?",
    "criteria": {
        "BUY": "Enter or add to a long position now: a Buy or Overweight call.",
        "HOLD": (
            "Take no new position: a Hold call, a call to only reduce exposure "
            "partially (Underweight), or no clear call."
        ),
        "SELL": "Exit the position or avoid the stock entirely: a Sell call.",
    },
}

EXIT_QUESTION = {
    "type": "choice",
    "instructions": "Does `analysis` conclude that the open long position should be sold now?",
    "criteria": {
        "SELL": "Close the position now: momentum has turned against it, or a loss should be cut.",
        "HOLD": "Keep the position open: the trend still supports it, or the signals are mixed.",
    },
}

# Scanner candidates: how strong is the screener's stated reason?  Levels are
# the positions in "criteria" (0-3).
CATALYST_QUESTIONS = {
    "catalyst": {
        "type": "score",
        "instructions": "How strong a reason to analyze `stock` today does `reason` give?",
        "criteria": [
            "None: vague or generic commentary, or an ordinary price move.",
            "Weak: a modest move or minor news that may not matter.",
            "Clear: a specific event, result, order or flow that could move the stock.",
            "Strong: major company-specific news, or an unusual move on heavy volume.",
        ],
    },
    "specific_news": {
        "type": "noul",
        "instructions": "`reason` names a specific event or piece of news about the company.",
        "criteria": {
            "true": (
                "It names results, an order, a deal, a rating change, a regulatory "
                "action or similar company news."
            ),
            "false": "It only describes price, volume or sentiment, or is generic.",
        },
    },
}


@dataclass(frozen=True)
class CatalystScore:
    """Jev's read of a scanner candidate's reason."""

    score: float  # expected catalyst level, 0 (none) to 3 (strong)
    confidence: float  # 0-1, how settled Jev is on the score
    specific_news: float  # probability the reason names concrete company news
    model: str


@dataclass(frozen=True)
class JevVerdict:
    """One Choice answer from Jev."""

    choice: str
    confidence: float  # 0-1, derived by Jev from the probability distribution
    probabilities: dict[str, float]
    model: str  # versioned model ID that answered, for the audit trail

    def probability(self, option: str) -> float:
        return float(self.probabilities.get(option, 0.0))

    def describe(self) -> str:
        """Compact form for logs and signal reasoning."""
        probs = ", ".join(f"{k}={v:.2f}" for k, v in sorted(self.probabilities.items()))
        return f"Jev {self.model}: {self.choice} (confidence {self.confidence:.2f}; {probs})"


class Jev:
    """Async access to Jev's System One endpoint.

    A client is opened per call, so the object is safe to share across the
    separate event loops the CLI and daemon create with ``asyncio.run``.

    Args:
        api_key: TypeSafe API key.
        model: Model ID; pin a version (``jev-1.13.0``) so tuned
            thresholds do not move when the ``jev-latest`` alias does.
        min_confidence: Confidence at or above which callers act on an answer.
        min_catalyst_score: Scanner candidates whose catalyst score (0-3) is
            below this are dropped.
        timeout: Seconds per attempt.
        transport: Optional ``httpx2`` transport (tests).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "jev-1.13.0",
        min_confidence: float = 0.6,
        min_catalyst_score: float = 1.0,
        timeout: float = 5.0,
        transport: Any = None,
    ) -> None:
        self.model = model
        self.min_confidence = min_confidence
        self.min_catalyst_score = min_catalyst_score
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    async def _system_one(self, state: Any, questions: dict[str, Any]) -> Any:
        """Send *questions* about *state*; the SDK response, or ``None`` on any failure."""
        try:
            from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

            async with AsyncTypeSafeClient(
                api_key=self._api_key,
                model=self.model,
                retry=RetryPolicy(max_retries=1, timeout=self._timeout),
                transport=self._transport,
            ) as client:
                return await client.system_one(state, questions)
        except Exception:
            logger.warning(
                "Jev request failed — falling back to the LLM's own answer", exc_info=True
            )
            return None

    async def ask(self, state: Any, question: dict[str, Any]) -> Optional[JevVerdict]:
        """Ask one Choice question about *state*; ``None`` on any failure."""
        response = await self._system_one(state, {"answer": question})
        try:
            answer = response.choices["answer"]
            return JevVerdict(
                choice=answer.choice,
                confidence=float(answer.confidence),
                probabilities={k: float(v) for k, v in answer.probabilities.items()},
                model=response.model,
            )
        except Exception:
            if response is not None:
                logger.warning("Jev returned no choice answer", exc_info=True)
            return None

    async def noul(self, state: Any, instructions: str) -> Optional[tuple[float, str]]:
        """Probability that *instructions* (a yes/no question) holds for *state*.

        Returns ``(probability_yes, model)``, or ``None`` on any failure.
        """
        response = await self._system_one(
            state, {"answer": {"type": "noul", "instructions": instructions}}
        )
        try:
            return float(response.nouls["answer"].noul), response.model
        except Exception:
            if response is not None:
                logger.warning("Jev returned no yes/no answer", exc_info=True)
            return None

    async def trade_action(self, decision: str) -> Optional[JevVerdict]:
        """BUY / HOLD / SELL probabilities for a Portfolio Manager decision."""
        if not decision.strip():
            return None
        return await self.ask({"decision": decision}, TRADE_QUESTION)

    async def exit_action(self, analysis: str) -> Optional[JevVerdict]:
        """SELL / HOLD probabilities for a sell analyst's analysis."""
        if not analysis.strip():
            return None
        return await self.ask({"analysis": analysis}, EXIT_QUESTION)

    async def catalyst(self, symbol: str, reason: str) -> Optional[CatalystScore]:
        """Catalyst strength of a scanner candidate's *reason*."""
        if not reason.strip():
            return None
        response = await self._system_one({"stock": symbol, "reason": reason}, CATALYST_QUESTIONS)
        try:
            score = response.scores["catalyst"]
            return CatalystScore(
                score=float(score.score),
                confidence=float(score.confidence),
                specific_news=float(response.nouls["specific_news"].noul),
                model=response.model,
            )
        except Exception:
            if response is not None:
                logger.warning("Jev returned incomplete catalyst answers", exc_info=True)
            return None


@lru_cache(maxsize=1)
def get_jev() -> Optional[Jev]:
    """The process-wide Jev, or ``None`` when disabled or unconfigured."""
    from skopaq.config import SkopaqConfig

    config = SkopaqConfig()
    if not config.jev_enabled:
        return None
    api_key = config.typesafe_api_key.get_secret_value()
    if not api_key:
        logger.warning("SKOPAQ_JEV_ENABLED is set but SKOPAQ_TYPESAFE_API_KEY is empty — Jev off")
        return None
    try:
        import typesafe_sdk  # noqa: F401
    except ImportError:
        logger.warning("typesafe-sdk is not installed — Jev off (pip install typesafe-sdk)")
        return None
    logger.info(
        "Jev enabled (model=%s, min_confidence=%.2f)", config.jev_model, config.jev_min_confidence
    )
    return Jev(
        api_key=api_key,
        model=config.jev_model,
        min_confidence=config.jev_min_confidence,
        min_catalyst_score=config.jev_min_catalyst_score,
        timeout=config.jev_timeout_seconds,
    )
