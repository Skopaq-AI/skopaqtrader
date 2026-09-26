"""Every production SafetyChecker.validate call passes ``sell_context=`` (finding 15).

``sell_context`` is what makes a live SELL count open SELL orders and refuse when the
order book cannot be read. It defaults to None (today's check, right for paper), so a
caller that forgets it would silently lose that protection in live mode. This test reads
the source instead of trusting every caller to remember.
"""

from __future__ import annotations

import ast
from pathlib import Path

import skopaq

SKOPAQ_DIR = Path(skopaq.__file__).parent


def _validate_calls() -> list[tuple[str, int, ast.Call]]:
    """``<expr>.validate(...)`` calls with at least 5 arguments (the SafetyChecker shape)."""
    found = []
    for path in sorted(SKOPAQ_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "validate"
                    and len(node.args) + len(node.keywords) >= 5):
                found.append((str(path.relative_to(SKOPAQ_DIR.parent)), node.lineno, node))
    return found


def test_every_safety_validate_call_passes_sell_context():   # T11
    calls = _validate_calls()
    # The Executor, MCP check_safety and place_order, and chat check_safety
    assert len(calls) >= 4, calls

    missing = [f"{path}:{line}" for path, line, node in calls
               if "sell_context" not in {kw.arg for kw in node.keywords}]
    assert not missing, f"SafetyChecker.validate called without sell_context= at {missing}"


def test_the_guard_notices_a_forgotten_argument():
    tree = ast.parse("safety.validate(order, signal, positions, funds, value, holdings=h)")
    [call] = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert "sell_context" not in {kw.arg for kw in call.keywords}
    assert len(call.args) + len(call.keywords) >= 5
