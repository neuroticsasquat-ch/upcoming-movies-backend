"""The sweep's boot-time settings check (NEU-1368).

The fault it exists for is silent: a quarantine window wider than the rolling lookback holds
every attachment until it ages out of the window it would be re-read from, so the phase cards
nothing and raises nothing. There is no run to inspect afterwards — the rows are simply gone.
"""

import pytest

from upmovies.config import get_settings
from upmovies.ingest.sweep import SweepConfigurationError, validate_sweep_configuration


def _settings(**overrides):
    return get_settings().model_copy(update=overrides)


def test_the_shipped_defaults_are_valid():
    """72 hours against a 7-day lookback. If this ever fails, one of the two defaults moved
    without the other."""
    validate_sweep_configuration(get_settings())


def test_a_window_wider_than_the_lookback_is_refused():
    settings = _settings(sweep_credit_quarantine_hours=8 * 24, sweep_event_lookback_days=7)

    with pytest.raises(SweepConfigurationError) as err:
        validate_sweep_configuration(settings)

    assert "SWEEP_CREDIT_QUARANTINE_HOURS" in str(err.value)
    assert "SWEEP_EVENT_LOOKBACK_DAYS" in str(err.value)


def test_a_window_equal_to_the_lookback_is_refused():
    """Equal is not safe: the row becomes eligible at the exact instant it leaves the
    window, and which of the two a pass sees is a race against the sweep's own clock."""
    settings = _settings(sweep_credit_quarantine_hours=7 * 24, sweep_event_lookback_days=7)

    with pytest.raises(SweepConfigurationError):
        validate_sweep_configuration(settings)


def test_a_window_inside_the_lookback_is_accepted():
    validate_sweep_configuration(
        _settings(sweep_credit_quarantine_hours=7 * 24 - 1, sweep_event_lookback_days=7)
    )


def test_zero_is_the_disable_switch_and_not_a_window():
    """0 means "card immediately", so it is exempt rather than compared — a lookback of any
    length is fine beside it."""
    validate_sweep_configuration(
        _settings(sweep_credit_quarantine_hours=0, sweep_event_lookback_days=1)
    )
