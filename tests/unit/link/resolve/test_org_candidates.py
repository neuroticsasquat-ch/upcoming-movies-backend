"""The pure half of organisation candidate generation: union, de-duplication, provenance
flags, the anchored-first cap and the popularity prior that rides along (EF-12)."""

from upmovies.ingest.tmdb.schemas import TMDBCollectionSearchHit, TMDBCompanySearchHit
from upmovies.link.resolve.org_candidates import (
    COLLECTION,
    COMPANY,
    ORG_CANDIDATE_CAP,
    OrgCandidate,
    build_org_candidates,
)


def company_hit(entity_id: int, **overrides) -> TMDBCompanySearchHit:
    row: dict = {
        "id": entity_id,
        "name": f"Studio {entity_id}",
        "logo_path": f"/logo{entity_id}.png",
        "origin_country": "US",
        "description": "an extra field we do not consume",
    }
    row.update(overrides)
    return TMDBCompanySearchHit.model_validate(row)


def collection_hit(entity_id: int, **overrides) -> TMDBCollectionSearchHit:
    row: dict = {
        "id": entity_id,
        "name": f"Franchise {entity_id} Collection",
        "original_name": f"Franchise {entity_id}",
        "poster_path": f"/poster{entity_id}.jpg",
        "backdrop_path": None,
        "adult": False,
    }
    row.update(overrides)
    return TMDBCollectionSearchHit.model_validate(row)


def attached(entity_id: int, kind: str = COMPANY, **overrides) -> OrgCandidate:
    fields: dict = {
        "entity_id": entity_id,
        "kind": kind,
        "name": f"Studio {entity_id}",
        "attached": True,
    }
    fields.update(overrides)
    return OrgCandidate(**fields)


def ids(candidates) -> list[int]:
    return [c.entity_id for c in candidates]


# --- the union -----------------------------------------------------------------------------


def test_unions_the_two_sources():
    candidates = build_org_candidates(
        kind=COMPANY, search_hits=[company_hit(1)], attached=[attached(2)]
    )
    assert sorted(ids(candidates)) == [1, 2]


def test_empty_sources_yield_no_candidates():
    assert build_org_candidates(kind=COMPANY, search_hits=[], attached=[]) == []


def test_one_organisation_found_by_both_sources_is_one_candidate():
    candidates = build_org_candidates(
        kind=COMPANY, search_hits=[company_hit(7)], attached=[attached(7)]
    )
    assert ids(candidates) == [7]
    assert candidates[0].from_search is True
    assert candidates[0].attached is True


def test_a_repeated_search_hit_is_kept_once():
    candidates = build_org_candidates(
        kind=COMPANY, search_hits=[company_hit(3), company_hit(3)], attached=[]
    )
    assert ids(candidates) == [3]


# --- provenance ----------------------------------------------------------------------------


def test_a_search_only_hit_is_not_attached():
    (candidate,) = build_org_candidates(kind=COMPANY, search_hits=[company_hit(4)], attached=[])
    assert (candidate.from_search, candidate.attached) == (True, False)


def test_a_film_row_the_search_missed_is_attached_and_not_from_search():
    (candidate,) = build_org_candidates(kind=COMPANY, search_hits=[], attached=[attached(5)])
    assert (candidate.from_search, candidate.attached) == (False, True)


def test_the_live_hit_name_wins_over_the_stored_one():
    """The hit was fetched for this mention; the catalog row was written whenever some film
    holding the company was last ingested."""
    (candidate,) = build_org_candidates(
        kind=COMPANY,
        search_hits=[company_hit(6, name="Blumhouse Productions")],
        attached=[attached(6, name="Blumhouse")],
    )
    assert candidate.name == "Blumhouse Productions"


def test_a_collection_hit_carries_its_original_name():
    (candidate,) = build_org_candidates(
        kind=COLLECTION,
        search_hits=[collection_hit(8, name="Le Samouraï Collection", original_name="Le Samouraï")],
        attached=[],
    )
    assert candidate.original_name == "Le Samouraï"


def test_a_company_hit_has_no_original_name():
    """TMDB's company records carry one spelling, so there is nothing to prefer."""
    (candidate,) = build_org_candidates(kind=COMPANY, search_hits=[company_hit(9)], attached=[])
    assert candidate.original_name is None


# --- the cap -------------------------------------------------------------------------------


def test_the_film_s_own_organisations_fill_the_cap_before_any_search_hit():
    candidates = build_org_candidates(
        kind=COMPANY,
        search_hits=[company_hit(n) for n in range(100, 120)],
        attached=[attached(1), attached(2)],
    )
    assert len(candidates) == ORG_CANDIDATE_CAP
    assert ids(candidates)[:2] == [1, 2]


def test_a_film_with_more_companies_than_the_cap_still_leaves_room_for_the_search():
    """Ten production companies is not rare, and unbounded the anchored tier would evict every
    search hit — so a studio the story named that is not yet on the film could never be a
    candidate at all."""
    candidates = build_org_candidates(
        kind=COMPANY,
        search_hits=[company_hit(100), company_hit(101)],
        attached=[attached(n) for n in range(1, 15)],
    )
    assert len(candidates) == ORG_CANDIDATE_CAP
    assert [c.entity_id for c in candidates if c.from_search] == [100, 101]


def test_the_reservation_never_evicts_an_organisation_both_sources_found():
    """Those carry both features at once, which is the strongest thing the union can say."""
    candidates = build_org_candidates(
        kind=COMPANY,
        search_hits=[company_hit(7), *(company_hit(n) for n in range(100, 110))],
        attached=[attached(n) for n in range(1, 8)],
    )
    assert 7 in ids(candidates)
    assert candidates[0].entity_id == 7


def test_the_reservation_is_half_the_cap_at_most():
    """A search that returned twenty hits does not get to push the film's own studios out."""
    candidates = build_org_candidates(
        kind=COMPANY,
        search_hits=[company_hit(n) for n in range(100, 120)],
        attached=[attached(n) for n in range(1, 15)],
    )
    assert len([c for c in candidates if c.attached]) == ORG_CANDIDATE_CAP // 2


def test_with_no_search_hits_the_anchored_tier_takes_the_whole_cap():
    """Nothing to reserve for, nothing reserved."""
    candidates = build_org_candidates(
        kind=COMPANY, search_hits=[], attached=[attached(n) for n in range(1, 15)]
    )
    assert len(candidates) == ORG_CANDIDATE_CAP


def test_the_cap_is_honoured_by_search_hits_alone():
    candidates = build_org_candidates(
        kind=COMPANY, search_hits=[company_hit(n) for n in range(1, 30)], attached=[]
    )
    assert len(candidates) == ORG_CANDIDATE_CAP


def test_the_cap_is_injectable():
    candidates = build_org_candidates(
        kind=COMPANY, search_hits=[company_hit(n) for n in range(1, 10)], attached=[], cap=3
    )
    assert ids(candidates) == [1, 2, 3]


# --- the popularity prior ------------------------------------------------------------------


def test_catalog_reach_is_attached_to_the_candidates_that_have_one():
    candidates = build_org_candidates(
        kind=COMPANY,
        search_hits=[company_hit(1), company_hit(2)],
        attached=[],
        catalog_reach={1: 42},
    )
    assert [(c.entity_id, c.catalog_reach) for c in candidates] == [(1, 42), (2, 0)]


def test_an_organisation_the_catalog_has_never_held_counts_zero():
    (candidate,) = build_org_candidates(
        kind=COMPANY, search_hits=[company_hit(1)], attached=[], catalog_reach={}
    )
    assert candidate.catalog_reach == 0
