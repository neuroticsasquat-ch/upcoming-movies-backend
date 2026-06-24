import pytest

from upmovies.link.cluster import is_stale_stage


@pytest.mark.parametrize("event_type", ["announced", "casting", "production_start"])
@pytest.mark.parametrize("status", ["Post Production", "Released"])
def test_early_stage_on_wrapped_film_is_stale(event_type, status):
    assert is_stale_stage(event_type, status) is True


@pytest.mark.parametrize("event_type", ["announced", "casting", "production_start"])
@pytest.mark.parametrize("status", ["In Production", "Planned", "Rumored", None])
def test_early_stage_on_unwrapped_film_is_not_stale(event_type, status):
    assert is_stale_stage(event_type, status) is False


@pytest.mark.parametrize("event_type", ["trailer", "release_date", "production_wrap", "other"])
@pytest.mark.parametrize("status", ["Post Production", "Released", "In Production", None])
def test_non_early_stage_types_are_never_stale(event_type, status):
    assert is_stale_stage(event_type, status) is False
