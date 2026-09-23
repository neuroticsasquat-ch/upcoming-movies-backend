"""The sweep's boot-time settings check (NEU-1368, NEU-1401).

The fault it exists for is silent: a quarantine window whose *effective* hold outlasts the
rolling lookback holds every attachment until it ages out of the window it would be re-read
from, so the phase cards nothing and raises nothing. There is no run to inspect afterwards —
the rows are simply gone.

The boundaries below are written as **literal** hour values rather than derived from
`SWEEP_HOLD_ROUNDING_HOURS`, deliberately (NEU-1401 D-1401.4): a test that computes its own
expectation from the constant cannot fail when the constant moves, which is the one thing
these tests exist to catch.
"""

import re

import pytest

from upmovies.config import get_settings
from upmovies.ingest.sweep import SweepConfigurationError, validate_sweep_configuration


def _settings(**overrides):
    return get_settings().model_copy(update=overrides)


def test_the_shipped_defaults_are_valid():
    """72 hours against a 7-day lookback. If this ever fails, one of the two defaults moved
    without the other."""
    validate_sweep_configuration(get_settings())


def test_the_spikes_recommended_window_is_valid():
    """NEU-1372 recommends 96h. It must clear the guard, or the recommendation is unshippable
    without also raising the lookback."""
    validate_sweep_configuration(
        _settings(sweep_credit_quarantine_hours=96, sweep_event_lookback_days=7)
    )


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


def test_a_window_one_hour_inside_the_lookback_is_refused():
    """167h against 168h was accepted before NEU-1401, on the nominal comparison. The hold is
    only ever observed at a sweep pass, so a one-hour margin is no margin at all."""
    with pytest.raises(SweepConfigurationError):
        validate_sweep_configuration(
            _settings(sweep_credit_quarantine_hours=7 * 24 - 1, sweep_event_lookback_days=7)
        )


def test_the_nominal_window_that_used_to_pass_is_refused():
    """NEU-1401's named case. 150 < 168, so the nominal comparison admitted it, but at a sweep
    at T the backlog only sees `changed_at` in [T-168h, T-150h] — an 18h band against a ~24h
    period, so rows landing in the remaining 6h are eligible at no pass at all."""
    settings = _settings(sweep_credit_quarantine_hours=150, sweep_event_lookback_days=7)

    with pytest.raises(SweepConfigurationError) as err:
        validate_sweep_configuration(settings)

    message = str(err.value)
    assert "150" in message
    assert "198" in message, "the effective hold must be named, not just the nominal value"
    assert "120" in message, "the ceiling at this lookback must be named"
    assert "SWEEP_EVENT_LOOKBACK_DAYS to 9" in message, (
        "the smallest lookback admitting 150h must be named as the remedy"
    )


def test_the_ceiling_is_accepted():
    """120h = 168 - 48. The largest hold whose effective hold still fits the window."""
    validate_sweep_configuration(
        _settings(sweep_credit_quarantine_hours=120, sweep_event_lookback_days=7)
    )


def test_one_hour_past_the_ceiling_is_refused():
    with pytest.raises(SweepConfigurationError):
        validate_sweep_configuration(
            _settings(sweep_credit_quarantine_hours=121, sweep_event_lookback_days=7)
        )


def test_zero_is_the_disable_switch_and_not_a_window():
    """0 means "card immediately", so it is exempt rather than compared — a lookback of any
    length is fine beside it."""
    validate_sweep_configuration(
        _settings(sweep_credit_quarantine_hours=0, sweep_event_lookback_days=1)
    )


def test_a_lookback_with_no_room_refuses_every_non_zero_hold():
    """`SWEEP_EVENT_LOOKBACK_DAYS` is `ge=1`, so a 1-day lookback has a ceiling of 24 - 48 =
    -24h. No non-zero hold is safe there, and the message must say so without printing a
    negative ceiling at the operator."""
    settings = _settings(sweep_credit_quarantine_hours=1, sweep_event_lookback_days=1)

    with pytest.raises(SweepConfigurationError) as err:
        validate_sweep_configuration(settings)

    message = str(err.value)
    assert "SWEEP_CREDIT_QUARANTINE_HOURS" in message
    assert not re.search(r"-\d", message), f"a negative number reached the message: {message}"
    assert "SWEEP_CREDIT_QUARANTINE_HOURS to 0" in message, (
        "the message must point at 0 (disable) as an option"
    )
