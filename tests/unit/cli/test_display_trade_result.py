"""display_trade_result: a live order that filled in part, or may still be working, says so.

Paper results (and live results that filled or were refused outright) look as before.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from skopaq.broker.models import ExecutionResult
from skopaq.cli import display
from skopaq.cli.theme import console


def _render(execution: ExecutionResult) -> str:
    result = SimpleNamespace(error=None, signal=None, execution=execution,
                             duration_seconds=1.0)
    with console.capture() as captured:
        display.display_trade_result(result)
    return captured.get()


def test_a_paper_fill_looks_as_before():
    out = _render(ExecutionResult(success=True, mode="paper", fill_price=2500.0))
    assert "FILLED" in out and "PAPER" in out and "2,500.00" in out
    assert "PARTIAL" not in out and "Orders" not in out


def test_a_refusal_looks_as_before():
    out = _render(ExecutionResult(success=False, mode="paper",
                                  rejection_reason="No short sales"))
    assert "REJECTED" in out and "No short sales" in out


def test_a_partial_live_fill_shows_how_much_filled():
    out = _render(ExecutionResult(
        success=True, mode="live", fill_price=95.0, filled_quantity=Decimal(3),
        requested_quantity=Decimal(5), outcome="partial", order_ids=["EQ-1"],
        broker_message="filled 3 of 5; rest cancelled"))
    assert "PARTIAL" in out and "3 of 5" in out
    assert "EQ-1" in out and "rest cancelled" in out


def test_a_live_order_that_may_still_be_working_is_unconfirmed():
    out = _render(ExecutionResult(
        success=False, mode="live", outcome="open", order_ids=["EQ-1"], remaining_open=True,
        rejection_reason="may still be working"))
    assert "UNCONFIRMED" in out and "REJECTED" not in out
    assert "EQ-1" in out and "may still be working" in out
