"""The read-only resolution queue (D-25): the admin gate, the path filter, the cursor, and
the decision shape the review page renders."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from tests.fixtures.catalog import add_film
from upmovies.catalog.models import Person
from upmovies.link.resolve.queue import DECISION_KINDS, encode_cursor
from upmovies.news.models import Story, StoryEntity, StoryPerson
from upmovies.routers.resolution_admin import DecisionKind

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
# As Pydantic renders it: UTC serializes with a `Z`, not a `+00:00` offset.
NOW_JSON = NOW.isoformat().replace("+00:00", "Z")

# One candidate exactly as `pipeline._candidate_log` writes it — the shape the page reads.
CANDIDATE = {
    "person_id": 1892,
    "name": "Chris Evans",
    "score": 0.62,
    "features": {"name_match": "exact", "already_credited": False, "popularity_prior": 0.1},
}

# One candidate exactly as `org_scoring.org_candidate_log` writes it (EF-12).
ORG_CANDIDATE = {
    "entity_id": 3172,
    "kind": "company",
    "name": "Blumhouse Productions",
    "score": 0.7,
    "features": {"name_match": "exact", "attached": True, "catalog_reach": 12},
}

# The same two as the DTO renders them: the stored shapes carry only the id column their own
# resolver writes, and the page fills the other in as null.
CANDIDATE_OUT = {**CANDIDATE, "entity_id": None, "kind": None}
ORG_CANDIDATE_OUT = {**ORG_CANDIDATE, "person_id": None}


async def _story(session: AsyncSession, film=None, **overrides) -> Story:
    fields: dict = {
        "source": "deadline",
        "outlet": "Deadline",
        "url": f"https://deadline.com/{overrides.pop('slug', 'a-story')}",
        "title": "A trade story",
        "film_id": film.id if film is not None else None,
        "link_status": "linked",
        "link_confidence": 0.9,
    }
    fields.update(overrides)
    story = Story(**fields)
    session.add(story)
    await session.flush()
    return story


async def _decision(session: AsyncSession, story: Story, **overrides) -> StoryPerson:
    """A mention the resolver has already decided — `path` and `resolved_at` both written."""
    fields: dict = {
        "story_id": story.id,
        "name_as_written": "Chris Evans",
        "role": "star",
        "department": "Acting",
        "evidence_span": "Chris Evans is set to star",
        "path": "unlinked",
        "confidence": 0.4,
        "features": {"title_mentioned": None, "event_type": "casting", "resolution": {}},
        "candidates": [CANDIDATE],
        "prompt_version": "2",
        "resolved_at": NOW,
    }
    fields.update(overrides)
    row = StoryPerson(**fields)
    session.add(row)
    await session.flush()
    await session.commit()
    return row


async def _org_decision(session: AsyncSession, story: Story, **overrides) -> StoryEntity:
    """An organisation mention the resolver has already decided (EF-12)."""
    fields: dict = {
        "story_id": story.id,
        "kind": "company",
        "name_as_written": "Blumhouse",
        "evidence_span": "Blumhouse is boarding the thriller",
        "path": "unlinked",
        "confidence": 0.5,
        "features": {
            "title_mentioned": None,
            "event_type": "company_attached",
            "resolution": {},
        },
        "candidates": [ORG_CANDIDATE],
        "prompt_version": "3",
        "resolved_at": NOW,
    }
    fields.update(overrides)
    row = StoryEntity(**fields)
    session.add(row)
    await session.flush()
    await session.commit()
    return row


# --- the gate ------------------------------------------------------------------------------


async def test_requires_auth(client):
    r = await client.get("/admin/resolution")
    assert r.status_code == 401


async def test_forbidden_for_non_admin(authed_client):
    r = await authed_client.get("/admin/resolution")
    assert r.status_code == 403


# --- the decision shape --------------------------------------------------------------------


async def test_returns_the_fields_the_review_page_renders(
    admin_authed_client, session: AsyncSession
):
    film = await add_film(session, tmdb_id=550, title="Fight Club")
    story = await _story(session, film)
    mention = await _decision(session, story)

    r = await admin_authed_client.get("/admin/resolution")
    assert r.status_code == 200
    body = r.json()
    assert body["next_cursor"] is None
    assert body["items"] == [
        {
            "id": str(mention.id),
            "story": {
                "id": str(story.id),
                "title": "A trade story",
                "url": story.url,
                "outlet": "Deadline",
            },
            "film": {"id": str(film.id), "tmdb_id": 550, "title": "Fight Club"},
            "name_as_written": "Chris Evans",
            "role": "star",
            "department": "Acting",
            "evidence_span": "Chris Evans is set to star",
            "path": "unlinked",
            "kind": "person",
            "entity_id": None,
            "person_id": None,
            "confidence": 0.4,
            "features": {"title_mentioned": None, "event_type": "casting", "resolution": {}},
            "candidates": [CANDIDATE_OUT],
            "resolved_at": NOW_JSON,
        }
    ]


async def test_keeps_the_whole_rejected_shortlist(admin_authed_client, session: AsyncSession):
    """The near-misses are the point of the page — not only the winner (D-25)."""
    story = await _story(session, await add_film(session, tmdb_id=551))
    runner_up = {**CANDIDATE, "person_id": 9999, "name": "Chris Evans", "score": 0.58}
    await _decision(session, story, path="tiebreak", candidates=[CANDIDATE, runner_up])

    r = await admin_authed_client.get("/admin/resolution")
    assert [c["person_id"] for c in r.json()["items"][0]["candidates"]] == [1892, 9999]


async def test_survives_a_story_whose_film_link_was_removed(
    admin_authed_client, session: AsyncSession
):
    story = await _story(session, None, link_status="rejected")
    await _decision(session, story)

    r = await admin_authed_client.get("/admin/resolution")
    assert r.status_code == 200
    assert r.json()["items"][0]["film"] is None


async def test_renders_a_malformed_candidate_rather_than_failing(
    admin_authed_client, session: AsyncSession
):
    """The queue's job is showing anomalies; a candidate missing a key is one of them."""
    story = await _story(session, await add_film(session, tmdb_id=552))
    await _decision(session, story, candidates=[{"person_id": 7}])

    r = await admin_authed_client.get("/admin/resolution")
    assert r.status_code == 200
    assert r.json()["items"][0]["candidates"] == [
        {
            "person_id": 7,
            "entity_id": None,
            "kind": None,
            "name": None,
            "score": None,
            "features": {},
        }
    ]


# --- filtering -----------------------------------------------------------------------------


async def test_filters_by_path(admin_authed_client, session: AsyncSession):
    film = await add_film(session, tmdb_id=553)
    for i, path in enumerate(("unlinked", "tiebreak", "accepted", "not_in_tmdb")):
        story = await _story(session, film, slug=f"story-{i}")
        await _decision(session, story, path=path, name_as_written=path)

    for path in ("unlinked", "tiebreak", "accepted", "not_in_tmdb"):
        r = await admin_authed_client.get(f"/admin/resolution?path={path}")
        assert r.status_code == 200
        assert [i["name_as_written"] for i in r.json()["items"]] == [path]


async def test_unfiltered_returns_every_path(admin_authed_client, session: AsyncSession):
    film = await add_film(session, tmdb_id=554)
    for i, path in enumerate(("unlinked", "tiebreak", "accepted", "not_in_tmdb")):
        story = await _story(session, film, slug=f"story-{i}")
        await _decision(session, story, path=path)

    r = await admin_authed_client.get("/admin/resolution")
    assert {i["path"] for i in r.json()["items"]} == {
        "unlinked",
        "tiebreak",
        "accepted",
        "not_in_tmdb",
    }


async def test_rejects_a_path_outside_the_vocabulary(admin_authed_client):
    r = await admin_authed_client.get("/admin/resolution?path=nonsense")
    assert r.status_code == 422


async def test_omits_mentions_awaiting_a_decision(admin_authed_client, session: AsyncSession):
    """An undecided mention has no features, no candidates and nothing to review."""
    story = await _story(session, await add_film(session, tmdb_id=555))
    await _decision(session, story, path=None, confidence=None, candidates=None, resolved_at=None)

    r = await admin_authed_client.get("/admin/resolution")
    assert r.json()["items"] == []


# --- paging --------------------------------------------------------------------------------


async def test_pages_newest_first_through_the_cursor(admin_authed_client, session: AsyncSession):
    film = await add_film(session, tmdb_id=556)
    for i in range(5):
        story = await _story(session, film, slug=f"story-{i}")
        await _decision(
            session, story, name_as_written=f"Person {i}", resolved_at=NOW + timedelta(minutes=i)
        )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        url = "/admin/resolution?limit=2" + (f"&cursor={cursor}" if cursor else "")
        body = (await admin_authed_client.get(url)).json()
        seen.extend(i["name_as_written"] for i in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == [f"Person {i}" for i in (4, 3, 2, 1, 0)]
    assert cursor is None


async def test_last_full_page_reports_no_next_cursor(admin_authed_client, session: AsyncSession):
    """Exactly `limit` rows left is the end, not a next page that turns out to be empty."""
    film = await add_film(session, tmdb_id=557)
    for i in range(2):
        story = await _story(session, film, slug=f"story-{i}")
        await _decision(session, story, resolved_at=NOW + timedelta(minutes=i))

    body = (await admin_authed_client.get("/admin/resolution?limit=2")).json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is None


async def test_cursor_breaks_ties_on_identical_timestamps(
    admin_authed_client, session: AsyncSession
):
    """Two decisions written by the same run share a `resolved_at`; the id orders them."""
    film = await add_film(session, tmdb_id=558)
    for i in range(3):
        story = await _story(session, film, slug=f"story-{i}")
        await _decision(session, story, name_as_written=f"Person {i}", resolved_at=NOW)

    first = (await admin_authed_client.get("/admin/resolution?limit=2")).json()
    second = (
        await admin_authed_client.get(f"/admin/resolution?limit=2&cursor={first['next_cursor']}")
    ).json()

    names = [i["name_as_written"] for i in first["items"] + second["items"]]
    assert sorted(names) == ["Person 0", "Person 1", "Person 2"]
    assert len(names) == len(set(names))


async def test_rejects_a_cursor_it_did_not_mint(admin_authed_client):
    r = await admin_authed_client.get("/admin/resolution?cursor=not-a-cursor")
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_cursor"


async def test_a_cursor_past_the_end_returns_an_empty_page(
    admin_authed_client, session: AsyncSession
):
    story = await _story(session, await add_film(session, tmdb_id=559))
    mention = await _decision(session, story)

    cursor = encode_cursor(NOW, mention.id)
    r = await admin_authed_client.get(f"/admin/resolution?cursor={cursor}")
    assert r.status_code == 200
    assert r.json() == {"items": [], "next_cursor": None}


async def test_naive_cursor_timestamp_is_rejected(admin_authed_client):
    """`encode_cursor` never mints one; a forged naive value would page from the wrong instant."""
    import base64
    from uuid import uuid4

    forged = base64.urlsafe_b64encode(f"2026-09-17T12:00:00|{uuid4()}".encode()).decode()
    r = await admin_authed_client.get(f"/admin/resolution?cursor={forged}")
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_cursor"


async def test_filter_and_cursor_apply_together(admin_authed_client, session: AsyncSession):
    """Page 2 of a filtered list must keep the filter, not just the keyset."""
    film = await add_film(session, tmdb_id=560)
    for i in range(3):
        story = await _story(session, film, slug=f"unlinked-{i}")
        await _decision(
            session, story, name_as_written=f"Unlinked {i}", resolved_at=NOW + timedelta(minutes=i)
        )
    for i in range(3):
        story = await _story(session, film, slug=f"accepted-{i}")
        await _decision(
            session,
            story,
            path="accepted",
            name_as_written=f"Accepted {i}",
            resolved_at=NOW + timedelta(minutes=i),
        )

    first = (await admin_authed_client.get("/admin/resolution?path=unlinked&limit=2")).json()
    second = (
        await admin_authed_client.get(
            f"/admin/resolution?path=unlinked&limit=2&cursor={first['next_cursor']}"
        )
    ).json()

    names = [i["name_as_written"] for i in first["items"] + second["items"]]
    assert names == ["Unlinked 2", "Unlinked 1", "Unlinked 0"]
    assert second["next_cursor"] is None


async def test_decided_row_with_no_candidates_renders_empty(
    admin_authed_client, session: AsyncSession
):
    """A cache hit decides without ranking anyone, leaving `candidates` NULL on a real row."""
    story = await _story(session, await add_film(session, tmdb_id=561))
    session.add(Person(id=1892, name="Chris Evans"))
    await session.flush()
    await _decision(session, story, path="accepted", person_id=1892, candidates=None)

    r = await admin_authed_client.get("/admin/resolution")
    assert r.status_code == 200
    assert r.json()["items"][0]["candidates"] == []


# --- the three kinds (EF-12) ----------------------------------------------------------------


async def test_defaults_to_the_person_queue(admin_authed_client, session: AsyncSession):
    """The page as it stands asks for no kind and keeps the queue it had, until NEU-1447."""
    film = await add_film(session, tmdb_id=570)
    story = await _story(session, film, slug="person-default")
    await _decision(session, story)
    await _org_decision(session, await _story(session, film, slug="org-default"))

    body = (await admin_authed_client.get("/admin/resolution")).json()
    assert [i["kind"] for i in body["items"]] == ["person"]


async def test_lists_the_company_queue(admin_authed_client, session: AsyncSession):
    film = await add_film(session, tmdb_id=571, title="Fight Club")
    story = await _story(session, film, slug="company")
    row = await _org_decision(session, story, path="accepted", entity_id=3172)

    body = (await admin_authed_client.get("/admin/resolution?kind=company")).json()
    assert body["items"] == [
        {
            "id": str(row.id),
            "kind": "company",
            "story": {
                "id": str(story.id),
                "title": "A trade story",
                "url": story.url,
                "outlet": "Deadline",
            },
            "film": {"id": str(film.id), "tmdb_id": 571, "title": "Fight Club"},
            "name_as_written": "Blumhouse",
            # Person facts, and `news.story_entity` has no column for them.
            "role": None,
            "department": None,
            "evidence_span": "Blumhouse is boarding the thriller",
            "path": "accepted",
            "entity_id": 3172,
            # The compatibility duplicate is person-only, so an organisation row leaves it null.
            "person_id": None,
            "confidence": 0.5,
            "features": {
                "title_mentioned": None,
                "event_type": "company_attached",
                "resolution": {},
            },
            "candidates": [ORG_CANDIDATE_OUT],
            "resolved_at": NOW_JSON,
        }
    ]


async def test_the_two_organisation_kinds_are_separate_queues(
    admin_authed_client, session: AsyncSession
):
    film = await add_film(session, tmdb_id=572)
    await _org_decision(session, await _story(session, film, slug="c"), name_as_written="Blumhouse")
    await _org_decision(
        session,
        await _story(session, film, slug="f"),
        kind="collection",
        name_as_written="The Conjuring",
    )

    companies = (await admin_authed_client.get("/admin/resolution?kind=company")).json()
    franchises = (await admin_authed_client.get("/admin/resolution?kind=collection")).json()
    assert [i["name_as_written"] for i in companies["items"]] == ["Blumhouse"]
    assert [i["name_as_written"] for i in franchises["items"]] == ["The Conjuring"]


async def test_the_path_filter_applies_within_a_kind(admin_authed_client, session: AsyncSession):
    film = await add_film(session, tmdb_id=573)
    for i, path in enumerate(("unlinked", "accepted")):
        story = await _story(session, film, slug=f"org-{i}")
        await _org_decision(session, story, path=path, name_as_written=path)

    body = (await admin_authed_client.get("/admin/resolution?kind=company&path=accepted")).json()
    assert [i["name_as_written"] for i in body["items"]] == ["accepted"]


async def test_an_organisation_page_pages_through_its_own_cursor(
    admin_authed_client, session: AsyncSession
):
    film = await add_film(session, tmdb_id=574)
    for i in range(3):
        story = await _story(session, film, slug=f"paged-org-{i}")
        await _org_decision(
            session, story, name_as_written=f"Studio {i}", resolved_at=NOW + timedelta(minutes=i)
        )

    first = (await admin_authed_client.get("/admin/resolution?kind=company&limit=2")).json()
    second = (
        await admin_authed_client.get(
            f"/admin/resolution?kind=company&limit=2&cursor={first['next_cursor']}"
        )
    ).json()
    names = [i["name_as_written"] for i in first["items"] + second["items"]]
    assert names == ["Studio 2", "Studio 1", "Studio 0"]
    assert second["next_cursor"] is None


async def test_rejects_a_kind_outside_the_vocabulary(admin_authed_client):
    r = await admin_authed_client.get("/admin/resolution?kind=studio")
    assert r.status_code == 422


def test_the_query_enum_spells_the_whole_vocabulary():
    """The enum is written out so FastAPI can document its members; this is what keeps the
    duplication honest."""
    assert tuple(k.value for k in DecisionKind) == DECISION_KINDS


async def test_an_undecided_organisation_is_omitted(admin_authed_client, session: AsyncSession):
    story = await _story(session, await add_film(session, tmdb_id=575), slug="undecided-org")
    await _org_decision(session, story, path=None, confidence=None, resolved_at=None)

    body = (await admin_authed_client.get("/admin/resolution?kind=company")).json()
    assert body["items"] == []


async def test_a_path_without_a_resolved_at_is_not_a_decision(
    admin_authed_client, session: AsyncSession
):
    """The backstop in `queue._decided()`: nothing pairs the two columns, and a NULL in the
    sort key would truncate a page rather than drop one row."""
    story = await _story(session, await add_film(session, tmdb_id=562))
    await _decision(session, story, resolved_at=None)

    r = await admin_authed_client.get("/admin/resolution")
    assert r.json()["items"] == []


async def test_limit_is_bounded(admin_authed_client):
    assert (await admin_authed_client.get("/admin/resolution?limit=0")).status_code == 422
    assert (await admin_authed_client.get("/admin/resolution?limit=201")).status_code == 422
