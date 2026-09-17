"""The pure half of candidate generation: union, de-duplication, provenance flags, cap."""

from datetime import UTC, datetime

from tests.fixtures.tmdb import make_person_search_hit
from upmovies.ingest.tmdb.schemas import TMDBPersonSearchHit
from upmovies.link.resolve.candidates import (
    CANDIDATE_CAP,
    CatalogPerson,
    ChangeFact,
    CreditFact,
    build_candidates,
)

CHANGED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def hit(person_id: int, **overrides) -> TMDBPersonSearchHit:
    return TMDBPersonSearchHit.model_validate(make_person_search_hit(person_id, **overrides))


def credited_person(person_id: int, **overrides) -> CatalogPerson:
    fields: dict = {
        "person_id": person_id,
        "name": f"Person {person_id}",
        "credits": (CreditFact(credit_type="cast", department="Acting", job=None, credit_order=1),),
    }
    fields.update(overrides)
    return CatalogPerson(**fields)


def changed_person(person_id: int, **overrides) -> CatalogPerson:
    fields: dict = {
        "person_id": person_id,
        "name": f"Person {person_id}",
        "changes": (
            ChangeFact(credit_type="crew", job="Director", change="added", changed_at=CHANGED_AT),
        ),
    }
    fields.update(overrides)
    return CatalogPerson(**fields)


def ids(candidates) -> list[int]:
    return [c.person_id for c in candidates]


def test_union_of_the_three_sources():
    candidates = build_candidates(
        search_hits=[hit(1)],
        credited=[credited_person(2)],
        change_stream=[changed_person(3)],
    )
    assert sorted(ids(candidates)) == [1, 2, 3]


def test_empty_sources_yield_no_candidates():
    assert build_candidates(search_hits=[], credited=[], change_stream=[]) == []


def test_one_person_in_all_three_sources_is_one_candidate_with_every_flag():
    candidates = build_candidates(
        search_hits=[hit(7)],
        credited=[credited_person(7)],
        change_stream=[changed_person(7)],
    )
    assert len(candidates) == 1
    only = candidates[0]
    assert only.person_id == 7
    assert (only.from_search, only.credited, only.in_change_stream) == (True, True, True)
    assert only.anchored is True


def test_search_only_candidate_carries_search_provenance_alone():
    only = build_candidates(search_hits=[hit(4)], credited=[], change_stream=[])[0]
    assert (only.from_search, only.credited, only.in_change_stream) == (True, False, False)
    assert only.anchored is False
    assert only.credits == ()
    assert only.changes == ()


def test_credited_candidate_carries_its_credit_facts():
    only = build_candidates(
        search_hits=[],
        credited=[
            credited_person(
                5,
                credits=(
                    CreditFact(
                        credit_type="crew",
                        department="Directing",
                        job="Director",
                        credit_order=None,
                    ),
                    CreditFact(credit_type="cast", department="Acting", job=None, credit_order=3),
                ),
            )
        ],
        change_stream=[],
    )[0]
    assert (only.from_search, only.credited, only.in_change_stream) == (False, True, False)
    assert [c.job for c in only.credits] == ["Director", None]
    assert [c.credit_order for c in only.credits] == [None, 3]
    assert [c.department for c in only.credits] == ["Directing", "Acting"]


def test_change_stream_candidate_carries_its_change_facts():
    only = build_candidates(
        search_hits=[],
        credited=[],
        change_stream=[
            changed_person(
                6,
                changes=(
                    ChangeFact(
                        credit_type="crew", job="Director", change="removed", changed_at=CHANGED_AT
                    ),
                ),
            )
        ],
    )[0]
    assert (only.from_search, only.credited, only.in_change_stream) == (False, False, True)
    assert [(c.change, c.job, c.changed_at) for c in only.changes] == [
        ("removed", "Director", CHANGED_AT)
    ]


def test_search_hit_person_facts_are_carried_through():
    only = build_candidates(
        search_hits=[hit(8, popularity=31.5, known_for_department="Directing")],
        credited=[],
        change_stream=[],
    )[0]
    assert only.name == "Person 8"
    assert only.original_name == "Person 8"
    assert only.popularity == 31.5
    assert only.known_for_department == "Directing"
    # `known_for` is the only filmography a person TMDB returned but the catalog has never
    # credited has — `make_person_search_hit` gives person N the title id 1000 + N.
    assert only.filmography_tmdb_ids == (1008,)


def test_live_search_facts_win_over_the_stored_person_row():
    """The search hit was fetched this second; `catalog.person` was written at the last
    ingest of some film. Where both hold a fact, the fresher one is the one to score on."""
    only = build_candidates(
        search_hits=[hit(9, name="Chris Evans", popularity=40.0, known_for_department="Acting")],
        credited=[
            credited_person(
                9, name="C. Evans", popularity=2.0, known_for_department="Sound", original_name="CE"
            )
        ],
        change_stream=[],
    )[0]
    assert only.name == "Chris Evans"
    assert only.popularity == 40.0
    assert only.known_for_department == "Acting"


def test_stored_person_row_fills_facts_the_search_hit_omits():
    only = build_candidates(
        search_hits=[hit(10, popularity=None, known_for_department=None, original_name=None)],
        credited=[
            credited_person(
                10, popularity=3.5, known_for_department="Writing", original_name="Stored Name"
            )
        ],
        change_stream=[],
    )[0]
    assert only.popularity == 3.5
    assert only.known_for_department == "Writing"
    assert only.original_name == "Stored Name"


def test_catalog_filmography_merges_with_known_for_without_repeats():
    only = build_candidates(
        search_hits=[hit(11)],
        credited=[credited_person(11)],
        change_stream=[],
        catalog_filmography={11: (1011, 550, 27205)},
    )[0]
    assert only.filmography_tmdb_ids == (1011, 550, 27205)


def test_catalog_filmography_reaches_a_candidate_no_search_hit_named():
    only = build_candidates(
        search_hits=[],
        credited=[credited_person(12)],
        change_stream=[],
        catalog_filmography={12: (550,)},
    )[0]
    assert only.filmography_tmdb_ids == (550,)


def test_cap_keeps_film_anchored_candidates_and_drops_search_overflow():
    candidates = build_candidates(
        search_hits=[hit(i) for i in range(100, 120)],
        credited=[credited_person(i) for i in range(1, 9)],
        change_stream=[changed_person(9)],
    )
    assert len(candidates) == CANDIDATE_CAP
    assert ids(candidates) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 100]
    assert all(c.anchored for c in candidates[:9])


def test_cap_truncates_anchored_candidates_too_once_they_fill_it():
    candidates = build_candidates(
        search_hits=[hit(100)],
        credited=[credited_person(i) for i in range(1, 13)],
        change_stream=[changed_person(50)],
    )
    assert ids(candidates) == list(range(1, 11))


def test_source_order_within_each_tier_is_preserved():
    candidates = build_candidates(
        search_hits=[hit(30), hit(31)],
        credited=[credited_person(20), credited_person(21)],
        change_stream=[changed_person(10), changed_person(11)],
    )
    assert ids(candidates) == [20, 21, 10, 11, 30, 31]


def test_a_candidate_credited_and_searched_keeps_its_anchored_position():
    """De-duplication must not demote a credited person to the search tier just because
    TMDB's name search also returned them — the cap is spent anchored-first."""
    candidates = build_candidates(
        search_hits=[hit(i) for i in range(100, 112)],
        credited=[credited_person(105)],
        change_stream=[],
        cap=2,
    )
    assert ids(candidates) == [105, 100]


def test_cap_is_configurable():
    candidates = build_candidates(
        search_hits=[hit(1), hit(2), hit(3)], credited=[], change_stream=[], cap=2
    )
    assert ids(candidates) == [1, 2]


def test_repeated_rows_for_one_person_within_a_source_collapse():
    candidates = build_candidates(
        search_hits=[hit(1), hit(1)],
        credited=[credited_person(2), credited_person(2)],
        change_stream=[changed_person(3), changed_person(3)],
    )
    assert ids(candidates) == [2, 3, 1]


def test_a_non_seed_grade_credit_claims_no_anchored_place():
    """`film_credit` holds the whole crew, so being on it cannot by itself be worth one of
    ten places — only a seed-grade credit is (`catalog.seed_grade`)."""
    candidates = build_candidates(
        search_hits=[],
        credited=[
            credited_person(
                1,
                credits=(
                    CreditFact(
                        credit_type="crew", department="Camera", job="Gaffer", credit_order=None
                    ),
                ),
            ),
            credited_person(
                2,
                credits=(
                    CreditFact(credit_type="cast", department="Acting", job=None, credit_order=30),
                ),
            ),
        ],
        change_stream=[],
    )
    assert candidates == []


def test_a_non_seed_grade_credit_still_flags_a_candidate_the_search_found():
    """Which is the point of keeping the whole credit list: D-21 scores "already credited"
    and "department vs role" for whoever is in the set, however they got there."""
    only = build_candidates(
        search_hits=[hit(1)],
        credited=[
            credited_person(
                1,
                credits=(
                    CreditFact(
                        credit_type="crew",
                        department="Sound",
                        job="Original Music Composer",
                        credit_order=None,
                    ),
                ),
            )
        ],
        change_stream=[],
    )[0]
    assert (only.credited, only.from_search) == (True, True)
    assert [c.job for c in only.credits] == ["Original Music Composer"]


def test_a_non_seed_grade_credit_does_not_evict_a_search_hit():
    candidates = build_candidates(
        search_hits=[hit(100), hit(101)],
        credited=[
            credited_person(
                i,
                credits=(
                    CreditFact(
                        credit_type="crew", department="Art", job="Set Dresser", credit_order=None
                    ),
                ),
            )
            for i in range(1, 30)
        ],
        change_stream=[],
        cap=2,
    )
    assert ids(candidates) == [100, 101]


def test_anchored_tier_is_ordered_strongest_attachment_first():
    candidates = build_candidates(
        search_hits=[],
        credited=[
            credited_person(
                1,
                credits=(
                    CreditFact(credit_type="cast", department="Acting", job=None, credit_order=0),
                ),
            ),
            credited_person(
                2,
                credits=(
                    CreditFact(
                        credit_type="crew", department="Writing", job="Writer", credit_order=None
                    ),
                ),
            ),
            credited_person(
                3,
                credits=(
                    CreditFact(
                        credit_type="crew",
                        department="Directing",
                        job="Director",
                        credit_order=None,
                    ),
                ),
            ),
            credited_person(
                4,
                credits=(
                    CreditFact(credit_type="cast", department="Acting", job=None, credit_order=4),
                ),
            ),
        ],
        change_stream=[],
    )
    assert ids(candidates) == [3, 2, 1, 4]


def test_a_person_who_directed_and_acted_ranks_by_the_stronger_credit():
    candidates = build_candidates(
        search_hits=[],
        credited=[
            credited_person(
                1,
                credits=(
                    CreditFact(credit_type="cast", department="Acting", job=None, credit_order=0),
                ),
            ),
            credited_person(
                2,
                credits=(
                    CreditFact(credit_type="cast", department="Acting", job=None, credit_order=2),
                    CreditFact(
                        credit_type="crew",
                        department="Directing",
                        job="Director",
                        credit_order=None,
                    ),
                ),
            ),
        ],
        change_stream=[],
    )
    assert ids(candidates) == [2, 1]
