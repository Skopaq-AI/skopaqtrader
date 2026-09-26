"""FillSettings and the shutdown budget: clamped config, and exits that fit the kill-after."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from skopaq.config import SkopaqConfig
from skopaq.execution.live_orders import FillSettings, shutdown_budget_seconds


def test_a_mock_config_gives_the_defaults():
    settings = FillSettings.from_config(MagicMock())
    assert settings == FillSettings()
    assert settings.shutdown_budget_s == 240.0
    assert settings.exit_worst_case_s == 99.0                  # 3 × (10 + 10 + 3) + 15 + 15


def test_out_of_range_values_are_clamped_with_a_warning(caplog):
    config = MagicMock(order_fill_poll_interval_seconds=0.1, order_exit_max_attempts=0,
                       order_fill_timeout_seconds=True)        # a bool is never a number here
    with caplog.at_level(logging.WARNING):
        settings = FillSettings.from_config(config)

    assert settings.poll_interval_s == 0.5
    assert settings.exit_max_attempts == 1
    assert settings.timeout_s == 30.0
    assert "SKOPAQ_ORDER_FILL_POLL_INTERVAL_SECONDS" in caplog.text
    assert "SKOPAQ_ORDER_EXIT_MAX_ATTEMPTS" in caplog.text


def test_the_fit_rule_at_the_maximum_clamps():
    config = MagicMock(order_exit_attempt_timeout_seconds=30,
                       order_cancel_confirm_timeout_seconds=30, order_exit_max_attempts=5,
                       order_reconcile_timeout_seconds=30)
    settings = FillSettings.from_config(config)

    assert (settings.exit_attempt_timeout_s, settings.exit_max_attempts) == (3.0, 2)
    assert settings.exit_worst_case_s <= settings.shutdown_budget_s / 2


def test_the_fit_rule_lowers_the_attempt_timeout_first():
    # 3 × (30 + 10 + 3) + 15 + 15 = 159 s > 120 s: 17 s attempts fit exactly
    settings = FillSettings.from_config(MagicMock(order_exit_attempt_timeout_seconds=30))
    assert (settings.exit_attempt_timeout_s, settings.exit_max_attempts) == (17.0, 3)


def test_a_short_kill_after_warns_that_exits_will_be_cut(caplog):
    with caplog.at_level(logging.WARNING):
        settings = FillSettings.from_config(MagicMock(scheduler_kill_after_seconds="60"))
    assert settings.shutdown_budget_s == 30.0
    assert (settings.exit_attempt_timeout_s, settings.exit_max_attempts) == (3.0, 1)
    assert "cut" in caplog.text


def test_from_a_real_config_with_env_strings(monkeypatch):
    monkeypatch.setenv("SKOPAQ_ORDER_FILL_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("SKOPAQ_ORDER_EXIT_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("SKOPAQ_ORDER_EXTRA_TERMINAL_STATUSES", "rejected by exchange, done")
    monkeypatch.setenv("SKOPAQ_INDSTOCKS_ORDER_REMARKS_ENABLED", "true")
    settings = FillSettings.from_config(SkopaqConfig(_env_file=None))

    assert settings.timeout_s == 45.0
    assert settings.exit_max_attempts == 4
    # 4 attempts: 4 × (10 + 10 + 3) + 30 = 122 s > 120 s, so each attempt gets 9.5 s
    assert settings.exit_attempt_timeout_s == 9.5
    assert settings.extra_terminal_statuses == frozenset({"REJECTED BY EXCHANGE", "DONE"})
    assert settings.remarks_enabled is True


@pytest.mark.parametrize("kill_after,margin,expected", [
    ("300", None, 240.0),
    ("60", None, 30.0),              # never below 30 s…
    ("30.0", None, 25.0),            # …nor within 5 s of the SIGKILL
    ("20", None, 15.0),
    ("abc", None, 240.0),            # unparseable: the scheduler's default 300
    ("0", None, 240.0),
    ("300", 10, 280.0),              # margin clamped to [20, 120]
    ("300", 500, 180.0),
    ("600", 90.0, 510.0),
])
def test_shutdown_budget(kill_after, margin, expected):
    config = MagicMock(scheduler_kill_after_seconds=kill_after)
    if margin is not None:
        config.order_shutdown_margin_seconds = margin
    assert shutdown_budget_seconds(config) == expected


def test_shutdown_budget_ignores_a_mock_kill_after():
    # TypeAdapter(int) would turn a MagicMock into 1 through __int__
    assert shutdown_budget_seconds(MagicMock()) == 240.0


def test_the_budget_ends_before_a_short_kill_after(caplog):
    # A 30 s floor would let order work run 10 s past a 20 s kill-after's SIGKILL
    config = MagicMock(scheduler_kill_after_seconds="20")
    assert shutdown_budget_seconds(config) == 15.0


@pytest.mark.parametrize("attr,value", [
    ("order_shutdown_margin_seconds", 500.0),
    ("order_shutdown_margin_seconds", float("inf")),
])
def test_an_ignored_or_clamped_margin_is_warned(caplog, attr, value):
    config = MagicMock(scheduler_kill_after_seconds="300")
    setattr(config, attr, value)
    with caplog.at_level(logging.WARNING):
        shutdown_budget_seconds(config)
    assert "SKOPAQ_ORDER_SHUTDOWN_MARGIN_SECONDS" in caplog.text


def test_a_non_finite_setting_is_warned(caplog):
    with caplog.at_level(logging.WARNING):
        settings = FillSettings.from_config(MagicMock(order_fill_timeout_seconds=float("nan")))
    assert settings.timeout_s == 30.0
    assert "SKOPAQ_ORDER_FILL_TIMEOUT_SECONDS" in caplog.text


# ── A typo in a setting must not stop api, telegram and the scheduler ─────────

MALFORMED = [
    ("SKOPAQ_ORDER_FILL_TIMEOUT_SECONDS", "30s", "order_fill_timeout_seconds", 30.0),
    ("SKOPAQ_ORDER_FILL_POLL_INTERVAL_SECONDS", "fast", "order_fill_poll_interval_seconds", 1.0),
    ("SKOPAQ_ORDER_CANCEL_CONFIRM_TIMEOUT_SECONDS", "",
     "order_cancel_confirm_timeout_seconds", 10.0),
    ("SKOPAQ_ORDER_EXIT_ATTEMPT_TIMEOUT_SECONDS", "10 s",
     "order_exit_attempt_timeout_seconds", 10.0),
    ("SKOPAQ_ORDER_EXIT_MAX_ATTEMPTS", "3.5", "order_exit_max_attempts", 3),
    ("SKOPAQ_ORDER_EXIT_REPRICE_BUFFER_PCT", "0.5%", "order_exit_reprice_buffer_pct", 0.5),
    ("SKOPAQ_ORDER_RECONCILE_TIMEOUT_SECONDS", "x", "order_reconcile_timeout_seconds", 15.0),
    ("SKOPAQ_ORDER_SHUTDOWN_MARGIN_SECONDS", "1m", "order_shutdown_margin_seconds", 60.0),
    ("SKOPAQ_ORDER_SELL_FILL_LAG_WINDOW_SECONDS", "10min",
     "order_sell_fill_lag_window_seconds", 600.0),
    ("SKOPAQ_ORDER_FILL_TIMEOUT_SECONDS", "inf", "order_fill_timeout_seconds", 30.0),
    ("SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK", "maybe", "allow_sell_without_order_book", False),
    ("SKOPAQ_INDSTOCKS_ORDER_REMARKS_ENABLED", "yes please",
     "indstocks_order_remarks_enabled", False),
    ("SKOPAQ_MONITOR_RESYNC_CYCLES", "", "monitor_resync_cycles", 3),
    ("SKOPAQ_MONITOR_RESYNC_CYCLES", "three", "monitor_resync_cycles", 3),
]


@pytest.mark.parametrize("env,value,attr,default", MALFORMED)
def test_a_malformed_setting_falls_back_to_its_default(monkeypatch, caplog, env, value, attr,
                                                        default):
    monkeypatch.setenv(env, value)
    with caplog.at_level(logging.WARNING):
        config = SkopaqConfig(_env_file=None)      # builds: every service can start

    assert getattr(config, attr) == default
    assert env in caplog.text
    settings = FillSettings.from_config(config)
    assert settings == FillSettings.from_config(SkopaqConfig(_env_file=None, **{attr: default}))


def test_a_malformed_override_leaves_the_order_book_check_on(monkeypatch):
    from skopaq.broker.paper_engine import PaperEngine
    from skopaq.execution.order_router import OrderRouter

    monkeypatch.setenv("SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK", "no-way")
    monkeypatch.setenv("SKOPAQ_TRADING_MODE", "live")
    router = OrderRouter(SkopaqConfig(_env_file=None), PaperEngine(), live_client=MagicMock())
    assert router.allows_sell_without_order_book is False


def test_well_formed_settings_still_parse(monkeypatch):
    monkeypatch.setenv("SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK", "true")
    monkeypatch.setenv("SKOPAQ_MONITOR_RESYNC_CYCLES", "5")
    monkeypatch.setenv("SKOPAQ_ORDER_EXIT_REPRICE_BUFFER_PCT", "1.5")
    config = SkopaqConfig(_env_file=None)
    assert config.allow_sell_without_order_book is True
    assert config.monitor_resync_cycles == 5
    assert config.order_exit_reprice_buffer_pct == 1.5


def test_a_clamped_resync_cycle_count_is_warned(caplog):
    from skopaq.execution.position_monitor import _resync_cycles

    with caplog.at_level(logging.WARNING):
        assert _resync_cycles(MagicMock(monitor_resync_cycles=500)) == 60
    assert "SKOPAQ_MONITOR_RESYNC_CYCLES" in caplog.text


def test_extra_terminal_statuses_cannot_redefine_known_ones(caplog):
    # PENDING or PARTIALLY FILLED declared "final" would stop the no-short-sale check from
    # counting open SELLs and make a resting exit look final: only statuses Skopaq does
    # not recognise are accepted
    config = MagicMock(order_extra_terminal_statuses="PENDING, partially filled, SUCCESS,"
                                                     " rejected by exchange, O-PENDING")
    with caplog.at_level(logging.WARNING):
        settings = FillSettings.from_config(config)
    assert settings.extra_terminal_statuses == frozenset({"REJECTED BY EXCHANGE"})
    assert "SKOPAQ_ORDER_EXTRA_TERMINAL_STATUSES" in caplog.text
    assert "PENDING" in caplog.text
