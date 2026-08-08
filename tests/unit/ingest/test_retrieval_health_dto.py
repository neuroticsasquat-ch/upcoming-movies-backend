"""`RetrievalHealthOut` — the rates the admin surface reads, derived from stored counts.

The counts are the state and the rates are views onto them (NEU-997), so these tests pin the
division rules rather than any stored rate. The one that matters is the empty denominator: a
run that retrieved nothing must report `None`, never `0.0` — a zero zero-candidate rate reads
as "retrieval is healthy", which is the opposite of what an absent denominator says.
"""

from upmovies.ingest.dto import RetrievalHealthOut


def _health(**overrides) -> RetrievalHealthOut:
    fields: dict = {
        "stories_retrieved": 200,
        "zero_candidate_stories": 50,
        "saturated_stories": 10,
        "mean_candidates": 2.5,
        "roster_picks": 20,
        "roster_picks_retrieved": 19,
    }
    fields.update(overrides)
    return RetrievalHealthOut(**fields)


def test_rates_are_fractions_of_the_stories_retrieved_denominator():
    health = _health()
    assert health.zero_candidate_rate == 0.25
    assert health.saturation_rate == 0.05


def test_recall_is_a_fraction_of_the_roster_picks_denominator():
    assert _health(roster_picks=4, roster_picks_retrieved=3).roster_pick_recall == 0.75


def test_rates_are_none_when_nothing_was_retrieved():
    health = _health(
        stories_retrieved=0, zero_candidate_stories=0, saturated_stories=0, mean_candidates=None
    )
    assert health.zero_candidate_rate is None
    assert health.saturation_rate is None


def test_recall_is_none_when_the_roster_linked_nothing():
    # Distinct from the retrieval denominator: a run can retrieve over hundreds of stories
    # and still give the roster nothing to be measured against.
    assert _health(roster_picks=0, roster_picks_retrieved=0).roster_pick_recall is None


def test_rates_are_serialized_alongside_the_counts():
    dumped = _health().model_dump()
    assert dumped["stories_retrieved"] == 200
    assert dumped["zero_candidate_rate"] == 0.25
    assert dumped["saturation_rate"] == 0.05
    assert dumped["roster_pick_recall"] == 0.95
