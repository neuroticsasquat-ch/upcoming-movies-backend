"""The reading half of candidate generation: the three sources against real rows.

The union, the flags and the cap are pinned in `tests/unit/link/resolve/test_candidates.py`,
which needs neither a database nor a network. What only a database can prove is here: which
credits count as a candidate, how far back the change stream reads, and that a person's
filmography comes back as TMDB ids.
"""

from datetime import UTC, datetime, timedelta

import httpx
import respx

from tests.fixtures.catalog import add_credit, add_film
from tests.fixtures.tmdb import make_person_search_hit
from upmovies.catalog.models import FilmCreditChange, Person
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.link.resolve.candidates import (
    gather_candidates,
    load_catalog_filmography,
    load_change_stream_people,
    load_credited_people,
)

BASE = "https://api.themoviedb.org/3"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _client() -> TMDBClient:
    return TMDBClient(
        base_url=BASE,
        api_key="test-key",
        rate_calls=50,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.001,
    )


def _search(query: str, hits: list[dict]) -> None:
    respx.get(f"{BASE}/search/person", params={"query": query, "page": 1}).mock(
        return_value=httpx.Response(
            200, json={"page": 1, "results": hits, "total_pages": 1, "total_results": len(hits)}
        )
    )


async def _change(session, film, person_id: int, **overrides) -> None:
    """One `film_credit_change` row, creating the person if this is their first."""
    if await session.get(Person, person_id) is None:
        session.add(Person(id=person_id, name=f"Person {person_id}"))
        await session.flush()
    fields: dict = {
        "credit_type": "crew",
        "job": "Director",
        "change": "added",
        "changed_at": NOW - timedelta(days=1),
    }
    fields.update(overrides)
    session.add(FilmCreditChange(film_id=film.id, person_id=person_id, **fields))
    await session.flush()


async def test_credited_source_is_every_current_credit(session):
    """Being credited is a fact about a person that D-21 scores, so the loader answers it for
    the whole crew. Which of them may *claim a place* in a set capped at ten is
    `build_candidates`' question, and only seed grade answers that one."""
    film = await add_film(session, 1)
    await add_credit(session, film, 10, credit_type="cast", credit_order=0)
    await add_credit(session, film, 12, credit_type="cast", credit_order=42)
    await add_credit(session, film, 20, credit_type="crew", job="Director")
    await add_credit(session, film, 22, credit_type="crew", job="Gaffer")

    people = await load_credited_people(session, film.id)

    assert [p.person_id for p in people] == [10, 12, 20, 22]
    gaffer = people[3]
    assert [(c.credit_type, c.job) for c in gaffer.credits] == [("crew", "Gaffer")]


async def test_credited_source_ignores_other_films(session):
    film = await add_film(session, 2)
    other = await add_film(session, 3)
    await add_credit(session, film, 10, credit_type="crew", job="Director")
    await add_credit(session, other, 11, credit_type="crew", job="Director")

    people = await load_credited_people(session, film.id)

    assert [p.person_id for p in people] == [10]


async def test_two_credits_for_one_person_collapse_into_one_candidate(session):
    film = await add_film(session, 4)
    await add_credit(session, film, 10, credit_type="crew", job="Director")
    await add_credit(session, film, 10, credit_type="crew", job="Writer")
    await add_credit(session, film, 10, credit_type="cast", credit_order=2)

    people = await load_credited_people(session, film.id)

    assert len(people) == 1
    assert sorted(c.job or "" for c in people[0].credits) == ["", "Director", "Writer"]
    assert [c.credit_order for c in people[0].credits if c.credit_type == "cast"] == [2]


async def test_credited_people_carry_their_stored_person_facts(session):
    film = await add_film(session, 5)
    await add_credit(session, film, 10, credit_type="crew", job="Director")
    person = await session.get(Person, 10)
    assert person is not None
    person.known_for_department = "Directing"
    person.popularity = 8.25
    person.original_name = "Stored Original"
    await session.flush()

    people = await load_credited_people(session, film.id)

    assert people[0].known_for_department == "Directing"
    assert people[0].popularity == 8.25
    assert people[0].original_name == "Stored Original"


async def test_change_stream_reads_the_window_and_both_directions(session):
    film = await add_film(session, 6)
    await _change(session, film, 10, changed_at=NOW - timedelta(days=1))
    await _change(session, film, 11, change="removed", changed_at=NOW - timedelta(days=13))
    await _change(session, film, 12, changed_at=NOW - timedelta(days=20))

    people = await load_change_stream_people(session, film.id, since=NOW - timedelta(days=14))

    assert [p.person_id for p in people] == [10, 11]
    assert [c.change for c in people[1].changes] == ["removed"]


async def test_change_stream_orders_most_recent_first_and_groups_by_person(session):
    film = await add_film(session, 7)
    await _change(session, film, 10, changed_at=NOW - timedelta(days=9))
    await _change(session, film, 11, changed_at=NOW - timedelta(days=5))
    await _change(session, film, 11, change="removed", changed_at=NOW - timedelta(days=2))

    people = await load_change_stream_people(session, film.id, since=NOW - timedelta(days=14))

    assert [p.person_id for p in people] == [11, 10]
    assert [c.change for c in people[0].changes] == ["removed", "added"]


async def test_change_stream_ignores_other_films(session):
    film = await add_film(session, 8)
    other = await add_film(session, 9)
    await _change(session, film, 10)
    await _change(session, other, 11)

    people = await load_change_stream_people(session, film.id, since=NOW - timedelta(days=14))

    assert [p.person_id for p in people] == [10]


async def test_filmography_is_tmdb_ids_across_every_catalog_film(session):
    one = await add_film(session, 550)
    two = await add_film(session, 27205)
    await add_credit(session, one, 10, credit_type="crew", job="Director")
    await add_credit(session, one, 10, credit_type="cast", credit_order=1)
    await add_credit(session, two, 10, credit_type="crew", job="Gaffer")
    await add_credit(session, two, 11, credit_type="cast", credit_order=0)

    filmography = await load_catalog_filmography(session, [10, 11, 12])

    assert filmography == {10: (550, 27205), 11: (27205,)}


async def test_filmography_of_nobody_is_no_query(session):
    assert await load_catalog_filmography(session, []) == {}


@respx.mock
async def test_gather_candidates_unions_all_three_sources(session):
    film = await add_film(session, 100)
    await add_credit(session, film, 10, credit_type="crew", job="Director")
    await _change(session, film, 11, change="removed")
    _search("Chris Evans", [make_person_search_hit(12), make_person_search_hit(10)])

    async with _client() as client:
        candidates = await gather_candidates(
            session, client, film_id=film.id, name_as_written="Chris Evans", now=NOW
        )

    assert [c.person_id for c in candidates] == [10, 11, 12]
    credited, changed, searched = candidates
    assert (credited.credited, credited.in_change_stream, credited.from_search) == (
        True,
        False,
        True,
    )
    assert (changed.credited, changed.in_change_stream, changed.from_search) == (False, True, False)
    assert (searched.credited, searched.in_change_stream, searched.from_search) == (
        False,
        False,
        True,
    )
    # 100 from the film this mention is on, 1012 from the search hit's `known_for`.
    assert searched.filmography_tmdb_ids == (1012,)
    assert credited.filmography_tmdb_ids == (1010, 100)


@respx.mock
async def test_gather_candidates_caps_the_union_anchored_first(session):
    film = await add_film(session, 101)
    for person_id in range(10, 16):
        await add_credit(session, film, person_id, credit_type="crew", job="Director")
    _search("Somebody", [make_person_search_hit(i) for i in range(200, 220)])

    async with _client() as client:
        candidates = await gather_candidates(
            session, client, film_id=film.id, name_as_written="Somebody", now=NOW
        )

    assert len(candidates) == 10
    assert [c.person_id for c in candidates[:6]] == list(range(10, 16))
    assert all(c.anchored for c in candidates[:6])
    assert [c.person_id for c in candidates[6:]] == [200, 201, 202, 203]


@respx.mock
async def test_gather_candidates_on_a_film_with_no_credits_is_search_only(session):
    film = await add_film(session, 102)
    _search("Nobody Known", [make_person_search_hit(30)])

    async with _client() as client:
        candidates = await gather_candidates(
            session, client, film_id=film.id, name_as_written="Nobody Known", now=NOW
        )

    assert [(c.person_id, c.anchored) for c in candidates] == [(30, False)]


@respx.mock
async def test_gather_candidates_with_no_search_hits_keeps_the_film_anchored_ones(session):
    film = await add_film(session, 103)
    await add_credit(session, film, 10, credit_type="cast", credit_order=0)
    _search("Unfindable", [])

    async with _client() as client:
        candidates = await gather_candidates(
            session, client, film_id=film.id, name_as_written="Unfindable", now=NOW
        )

    assert [(c.person_id, c.from_search, c.credited) for c in candidates] == [(10, False, True)]


@respx.mock
async def test_a_non_seed_grade_credit_flags_a_search_hit_but_claims_no_place(session):
    """The composer the article names arrives through TMDB's name search, not on the strength
    of a credit no cap of ten can afford to honour — but arrives flagged `credited`, with the
    credit itself attached, because that is what D-21's "already credited" feature reads."""
    film = await add_film(session, 104)
    await add_credit(session, film, 40, credit_type="crew", job="Original Music Composer")
    _search("Ludwig Goransson", [make_person_search_hit(40)])

    async with _client() as client:
        candidates = await gather_candidates(
            session, client, film_id=film.id, name_as_written="Ludwig Goransson", now=NOW
        )

    assert [c.person_id for c in candidates] == [40]
    assert (candidates[0].credited, candidates[0].from_search) == (True, True)
    assert [c.job for c in candidates[0].credits] == ["Original Music Composer"]


@respx.mock
async def test_a_non_seed_grade_credit_alone_is_not_a_candidate(session):
    film = await add_film(session, 105)
    await add_credit(session, film, 41, credit_type="crew", job="Gaffer")
    await add_credit(session, film, 42, credit_type="cast", credit_order=30)
    await add_credit(session, film, 43, credit_type="crew", job="Director")
    _search("Somebody Else", [])

    async with _client() as client:
        candidates = await gather_candidates(
            session, client, film_id=film.id, name_as_written="Somebody Else", now=NOW
        )

    assert [c.person_id for c in candidates] == [43]


@respx.mock
async def test_anchored_tier_orders_by_strongest_attachment(session):
    """`catalog.seed_grade.ROLE_ORDER` — director, writer, cast — is what decides who the cap
    keeps, rather than an ordering this module invents."""
    film = await add_film(session, 106)
    await add_credit(session, film, 50, credit_type="cast", credit_order=0)
    await add_credit(session, film, 51, credit_type="crew", job="Screenplay")
    await add_credit(session, film, 52, credit_type="crew", job="Director")
    await add_credit(session, film, 53, credit_type="cast", credit_order=3)
    _search("Anyone", [])

    async with _client() as client:
        candidates = await gather_candidates(
            session, client, film_id=film.id, name_as_written="Anyone", now=NOW
        )

    assert [c.person_id for c in candidates] == [52, 51, 50, 53]
