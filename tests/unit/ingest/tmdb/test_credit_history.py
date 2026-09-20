"""The recorded-credit diff, and the rule that governs it: first observation is a baseline.

These are the pure-function half of NEU-1082. The integration half — that the rule survives
an actual `upsert_film` round trip through the delete-and-reinsert rebuild — lives in
`tests/integration/ingest/tmdb/test_credit_history.py`.
"""

from tests.fixtures.tmdb import make_details
from upmovies.ingest.tmdb.credit_history import (
    CREDIT_ADDED,
    CREDIT_REMOVED,
    CreditChange,
    RecordedCredit,
    diff_recorded_credits,
    recorded_credits_from_details,
)
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails

DIRECTOR = RecordedCredit(person_id=525, credit_type="crew", job="Director")
WRITER = RecordedCredit(person_id=525, credit_type="crew", job="Writer")
LEAD = RecordedCredit(person_id=6193, credit_type="cast", job=None)


def test_first_observation_writes_nothing():
    """THE headline property (ADR-0014, spec §5.3). `previous is None` means the catalog has
    never held a credit for this film, and a first observation is a baseline, never a change —
    whatever the incoming set contains."""
    assert diff_recorded_credits(previous=None, current={DIRECTOR, WRITER, LEAD}) == []


def test_first_observation_of_an_empty_set_writes_nothing():
    assert diff_recorded_credits(previous=None, current=set()) == []


def test_added_seed_credit_is_recorded():
    changes = diff_recorded_credits(previous={LEAD}, current={LEAD, DIRECTOR})
    assert changes == [CreditChange(credit=DIRECTOR, change=CREDIT_ADDED)]


def test_removed_seed_credit_is_recorded():
    changes = diff_recorded_credits(previous={LEAD, DIRECTOR}, current={LEAD})
    assert changes == [CreditChange(credit=DIRECTOR, change=CREDIT_REMOVED)]


def test_unchanged_set_writes_nothing():
    """The subtle one: the rebuild deletes and reinserts unconditionally, so the diff must be
    over set membership, not over the write operations the rebuild performs."""
    assert diff_recorded_credits(previous={DIRECTOR, LEAD}, current={LEAD, DIRECTOR}) == []


def test_a_film_observed_with_no_seed_credits_is_not_a_baseline():
    """An empty `previous` set is not the same as `previous is None`. The film was observed
    holding only non-seed credits; a director arriving now is a genuine attachment."""
    changes = diff_recorded_credits(previous=set(), current={DIRECTOR})
    assert changes == [CreditChange(credit=DIRECTOR, change=CREDIT_ADDED)]


def test_one_person_directing_and_writing_is_two_credits():
    """Identity is (person, credit_type, job) — the same person holds two seed-grade credits
    on a film they both wrote and directed, and dropping one is a change."""
    changes = diff_recorded_credits(previous={DIRECTOR, WRITER}, current={DIRECTOR})
    assert changes == [CreditChange(credit=WRITER, change=CREDIT_REMOVED)]


def test_changes_are_ordered_deterministically():
    """Additions before removals, each ordered by (person, credit_type, job) — so a run's
    rows land in a stable order rather than a set-iteration one."""
    changes = diff_recorded_credits(previous={DIRECTOR}, current={WRITER, LEAD})
    assert changes == [
        CreditChange(credit=WRITER, change=CREDIT_ADDED),
        CreditChange(credit=LEAD, change=CREDIT_ADDED),
        CreditChange(credit=DIRECTOR, change=CREDIT_REMOVED),
    ]


# --- recorded_credits_from_details ------------------------------------------------------------


def _credits(cast: list[dict] | None = None, crew: list[dict] | None = None) -> dict:
    return {"cast": cast or [], "crew": crew or []}


def _cast(person_id: int, order: int) -> dict:
    return {
        "id": person_id,
        "name": f"person-{person_id}",
        "credit_id": f"cast-{person_id}",
        "order": order,
        "character": "Someone",
    }


def _crew(person_id: int, job: str, department: str = "Directing") -> dict:
    return {
        "id": person_id,
        "name": f"person-{person_id}",
        "credit_id": f"crew-{person_id}-{job}",
        "job": job,
        "department": department,
    }


def _details(credits: dict) -> TMDBMovieDetails:
    return TMDBMovieDetails.model_validate(make_details(1, credits=credits))


def test_seed_credits_keeps_only_seed_grade_roles():
    details = _details(
        _credits(
            cast=[_cast(1, 0), _cast(2, 4), _cast(3, 5), _cast(4, 40)],
            crew=[
                _crew(10, "Director"),
                _crew(11, "Writer", "Writing"),
                _crew(12, "Screenplay", "Writing"),
                _crew(13, "Gaffer", "Lighting"),
                _crew(14, "Executive Producer", "Production"),
            ],
        )
    )
    assert recorded_credits_from_details(details) == {
        RecordedCredit(person_id=1, credit_type="cast", job=None),
        RecordedCredit(person_id=2, credit_type="cast", job=None),
        RecordedCredit(person_id=10, credit_type="crew", job="Director"),
        RecordedCredit(person_id=11, credit_type="crew", job="Writer"),
        RecordedCredit(person_id=12, credit_type="crew", job="Screenplay"),
    }


def test_cast_without_an_order_is_not_top_billed():
    details = _details(_credits(cast=[_cast(1, 0) | {"order": None}]))
    assert recorded_credits_from_details(details) == set()


def test_details_without_credits_yields_nothing():
    details = TMDBMovieDetails.model_validate(make_details(1))
    assert recorded_credits_from_details(details) == set()


def test_a_film_observed_holding_nothing_is_not_a_baseline():
    """The distinction the durable `film.credits_observed_at` marker exists to preserve. A
    speculative TMDB entry can be admitted with an empty credits payload; inferring "never
    observed" from "holds no credits" would make it baseline again next run and swallow the
    first director to attach."""
    assert diff_recorded_credits(previous=set(), current={DIRECTOR}) == [
        CreditChange(credit=DIRECTOR, change=CREDIT_ADDED)
    ]


# --- recorded grade: seed grade, or a follow (D-49) ------------------------------------------


def test_a_followed_person_has_every_credit_recorded():
    """Their non-seed cast and crew credits join the recorded set; nobody else's do."""
    details = _details(
        _credits(
            cast=[_cast(1, 0), _cast(2, 40)],
            crew=[_crew(13, "Gaffer", "Lighting"), _crew(14, "Best Boy", "Lighting")],
        )
    )

    assert recorded_credits_from_details(details, followed={2, 13}) == {
        RecordedCredit(person_id=1, credit_type="cast", job=None),
        RecordedCredit(person_id=2, credit_type="cast", job=None),
        RecordedCredit(person_id=13, credit_type="crew", job="Gaffer"),
    }


def test_no_followed_set_is_exactly_seed_grade():
    """The default every pre-M9 caller gets, spelled as an assertion rather than left to the
    signature: `followed=()` must not widen anything."""
    details = _details(_credits(cast=[_cast(2, 40)], crew=[_crew(13, "Gaffer", "Lighting")]))

    assert recorded_credits_from_details(details) == set()


def test_a_follow_appearing_between_observations_records_no_phantom_change():
    """The failure the "same set on both sides" rule exists to prevent (D-49). A 40th-billed
    credit that was present all along is in `previous` and in `current` alike once the follow
    exists, because both sides are judged by the same followed set at the same moment — so the
    diff is empty rather than a fabricated `added`."""
    details = _details(_credits(cast=[_cast(2, 40)]))
    followed = {2}
    stored = recorded_credits_from_details(details, followed=followed)

    assert diff_recorded_credits(previous=stored, current=stored) == []


def test_a_followed_persons_credit_leaving_is_a_removal():
    """Detachments follow the same rule as attachments: the row is recorded, so its
    disappearance is history like a director's."""
    minor = RecordedCredit(person_id=2, credit_type="cast", job=None)

    assert diff_recorded_credits(previous={minor}, current=set()) == [
        CreditChange(credit=minor, change=CREDIT_REMOVED)
    ]


def test_first_observation_is_still_a_baseline_for_a_followed_person():
    """The headline property is about `previous is None`, not about the grade, so widening
    what is recorded must not widen what a baseline emits."""
    minor = RecordedCredit(person_id=2, credit_type="cast", job=None)

    assert diff_recorded_credits(previous=None, current={minor}) == []
