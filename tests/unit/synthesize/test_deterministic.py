from datetime import date

import pytest

from upmovies.synthesize.deterministic import (
    DETERMINISTIC_MODEL,
    TEMPLATE_VERSION,
    CreditAttached,
    ReleaseDateMoved,
    ReleaseDateSet,
    StatusChanged,
    render_summary,
)


def test_release_date_set_renders_the_new_date_in_full():
    assert render_summary(ReleaseDateSet(new_date=date(2026, 8, 14))) == (
        "Release date set to 14 August 2026."
    )


def test_release_date_set_does_not_zero_pad_the_day():
    assert render_summary(ReleaseDateSet(new_date=date(2026, 8, 2))) == (
        "Release date set to 2 August 2026."
    )


def test_release_date_moved_names_both_dates():
    assert (
        render_summary(
            ReleaseDateMoved(previous_date=date(2026, 8, 14), new_date=date(2026, 10, 2))
        )
        == "Release date moved from 14 August 2026 to 2 October 2026."
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("In Production", "The film has entered production."),
        ("Post Production", "The film has entered post-production."),
        ("Released", "The film has been released."),
        ("Canceled", "The film has been canceled."),
        ("Planned", "The film is now listed as planned."),
        ("Rumored", "The film is now listed as rumored."),
    ],
)
def test_status_change_renders_its_template(status, expected):
    assert render_summary(StatusChanged(new_status=status)) == expected


def test_unknown_status_falls_back_to_naming_it():
    """TMDB can add a status; a catalog event must still get a body rather than crash the
    stage that creates it — an event with no summary row is invisible everywhere (§5.4)."""
    assert render_summary(StatusChanged(new_status="Shelved")) == (
        "The film's production status is now Shelved."
    )


def test_director_attached():
    assert render_summary(CreditAttached(role="director", name="Denis Villeneuve")) == (
        "Denis Villeneuve attached to direct."
    )


def test_writer_attached():
    assert render_summary(CreditAttached(role="writer", name="Jon Spaihts")) == (
        "Jon Spaihts attached to write."
    )


def test_cast_attached_without_a_character():
    assert render_summary(CreditAttached(role="cast", name="Zendaya")) == (
        "Zendaya joins the cast."
    )


def test_cast_attached_with_a_character():
    assert render_summary(CreditAttached(role="cast", name="Zendaya", character="Chani")) == (
        "Zendaya joins the cast as Chani."
    )


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError, match="producer"):
        render_summary(CreditAttached(role="producer", name="Mary Parent"))


def test_sentinel_model_is_not_a_real_model_id():
    """`model` must never be a real model id: pricing keys on (provider, model) and the cost
    ledger must not carry rows for a call that was never made (ADR-0014)."""
    assert DETERMINISTIC_MODEL == "deterministic"
    assert TEMPLATE_VERSION.startswith("deterministic-")
