"""`GET /me/timeline` (D-11, D-12): `/feed/grouped` filtered by the user's follows, behind the
entitlement gate (D-39).

Films are created undated (`release_date=None`) wherever a person follow is in play, because
`make_film`'s default release date is a fixed day in 2026 that the suite will walk past — a
person follow only reaches films still *in play*, so a dated fixture would start passing or
failing on the calendar rather than on the rule under test.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from tests.fixtures.public import ref
from upmovies.app.models import WatchlistDismissal
from upmovies.news.models import EventStory, StoryPerson


@pytest.fixture
def follow(entitled_client):
    """Follow an entity through the real route, so a test's setup exercises the same
    normalisation and existence checks a user's follow button does."""

    async def _follow(entity_type: str, entity_id: str | int) -> None:
        r = await entitled_client.post(
            "/me/follows", json={"entity_type": entity_type, "entity_id": str(entity_id)}
        )
        assert r.status_code in (200, 201), r.json()

    return _follow


@pytest.fixture
def name_in_story(session):
    """Write the resolver's own row (D-24): one person named in the story behind an event.

    Takes the event rather than the story because that is the direction the timeline reads in,
    and `add_event(sources=...)` is what puts a story there in the first place.
    """

    async def _name(event, *, person_id: int | None, path: str, name: str = "A Person") -> None:
        story_id = await session.scalar(
            select(EventStory.story_id).where(EventStory.event_id == event.id)
        )
        session.add(
            StoryPerson(
                story_id=story_id,
                person_id=person_id,
                name_as_written=name,
                path=path,
                prompt_version="1",
            )
        )
        await session.commit()

    return _name


# --- the gate ------------------------------------------------------------------------------


async def test_timeline_requires_auth(client):
    r = await client.get("/me/timeline")
    assert r.status_code == 401


async def test_timeline_is_403_for_an_unentitled_user(authed_client, make_film, add_event):
    # `authed_client`'s user has `entitled_until` NULL — the state every signup starts in
    # (D-37). 403 rather than an empty feed, so the client can render the D-41 locked panel.
    film = await make_film(slug="film-2026")
    await add_event(film=film, summary="a")

    r = await authed_client.get("/me/timeline")
    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"


# --- what the follow graph reaches ----------------------------------------------------------


async def test_timeline_of_an_empty_follow_graph_is_empty(entitled_client, make_film, add_event):
    film = await make_film(slug="film-2026")
    await add_event(film=film, summary="a")

    body = (await entitled_client.get("/me/timeline")).json()
    assert body["items"] == []
    assert body["total"] == 0


async def test_following_a_director_shows_only_their_films(
    entitled_client, make_film, add_event, attach_credits, follow
):
    theirs = await make_film(slug="theirs", title="Theirs", release_date=None)
    someone_elses = await make_film(slug="not-theirs", title="Not Theirs", release_date=None)
    await attach_credits(theirs, crew=[{"id": 525, "name": "C. Nolan", "job": "Director"}])
    await attach_credits(
        someone_elses, crew=[{"id": 1032, "name": "M. Scorsese", "job": "Director"}]
    )
    await add_event(film=theirs, summary="theirs", created_at=datetime(2026, 6, 3, tzinfo=UTC))
    await add_event(film=someone_elses, summary="not", created_at=datetime(2026, 6, 3, tzinfo=UTC))

    await follow("person", 525)

    body = (await entitled_client.get("/me/timeline")).json()
    assert [i["film_ref"] for i in body["items"]] == [ref(theirs)]
    assert body["total"] == 1


async def test_every_credit_counts_whatever_its_grade(
    entitled_client, session, make_film, add_event, follow
):
    """D-11's seed-grade cut on the person branch, gone (EF-2).

    The three grades `catalog/seed_grade.py` defines, one film each, plus the three that are
    deliberately *not* seed grade: a 6th-billed role, an unbilled one (TMDB leaves `order` off
    the long tail, which is exactly the cut) and a producer credit. All six are on the timeline
    now — a follow is binary, and the bottom three are the ones it used to silently decline.
    The credits are written directly because `attach_credits` inserts the person per call, and
    this is one person credited on six films."""
    from upmovies.catalog.models import FilmCredit, Person

    session.add(Person(id=287, name="A Person"))
    await session.flush()

    grades = {
        "Director": {"credit_type": "crew", "job": "Director", "department": "Directing"},
        "Writer": {"credit_type": "crew", "job": "Screenplay", "department": "Writing"},
        "Top Billed": {"credit_type": "cast", "credit_order": 4},
        "Sixth Billed": {"credit_type": "cast", "credit_order": 5},
        "Unbilled": {"credit_type": "cast", "credit_order": None},
        "Producer": {"credit_type": "crew", "job": "Producer", "department": "Production"},
    }
    for title, credit in grades.items():
        film = await make_film(slug=title.lower().replace(" ", "-"), title=title, release_date=None)
        session.add(FilmCredit(credit_id=f"c-{film.id}", film_id=film.id, person_id=287, **credit))
        await add_event(film=film, summary="s", created_at=datetime(2026, 6, 3, tzinfo=UTC))
    await session.commit()

    await follow("person", 287)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert sorted(i["film_title"] for i in items) == sorted(grades)


@pytest.mark.parametrize(
    "out_of_play",
    [
        pytest.param({"release_date": date(2020, 1, 1)}, id="already released"),
        pytest.param({"status": "Canceled", "release_date": None}, id="canceled"),
    ],
)
async def test_a_person_follow_does_not_reach_films_out_of_play(
    entitled_client, make_film, add_event, attach_credits, follow, out_of_play
):
    # D-11 scopes a person follow to films in play: following a director is an interest in what
    # they are making next, not in their back catalogue.
    film = await make_film(slug="old", **out_of_play)
    await attach_credits(film, crew=[{"id": 525, "name": "C. Nolan", "job": "Director"}])
    await add_event(film=film, summary="a")

    await follow("person", 525)

    assert (await entitled_client.get("/me/timeline")).json()["items"] == []


async def test_a_title_follow_reaches_a_released_film(
    entitled_client, make_film, add_event, follow
):
    # The in-play cut belongs to the person branch alone — following a title is a request for
    # that specific film, whatever state it is in.
    film = await make_film(slug="released", status="Released", release_date=date(2020, 1, 1))
    await add_event(film=film, summary="a")

    await follow("title", film.id)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(film)]


async def test_a_company_follow_reaches_a_released_film(
    entitled_client, make_film, add_event, attach_companies, follow
):
    # As with a title: only the person branch carries the in-play cut, so a company's back
    # catalogue still reaches the timeline.
    film = await make_film(slug="released", status="Released", release_date=date(2020, 1, 1))
    await attach_companies(film, [(508, "Regency")])
    await add_event(film=film, summary="a")

    await follow("company", 508)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(film)]


async def test_a_company_follow_reaches_its_films(
    entitled_client, make_film, add_event, attach_companies, follow
):
    theirs = await make_film(slug="theirs", title="Theirs")
    someone_elses = await make_film(slug="other", title="Other")
    await attach_companies(theirs, [(508, "Regency")])
    await attach_companies(someone_elses, [(4, "Paramount")])
    await add_event(film=theirs, summary="a")
    await add_event(film=someone_elses, summary="b")

    await follow("company", 508)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(theirs)]


async def test_a_franchise_follow_reaches_its_collection(
    entitled_client, make_film, add_event, make_collection, follow
):
    await make_collection(id=10, name="A Collection")
    inside = await make_film(slug="inside", title="Inside", collection_id=10)
    outside = await make_film(slug="outside", title="Outside")
    await add_event(film=inside, summary="a")
    await add_event(film=outside, summary="b")

    await follow("franchise", 10)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(inside)]


async def test_a_film_matched_by_two_follows_appears_once(
    entitled_client, make_film, add_event, attach_companies, follow
):
    film = await make_film(slug="both", title="Both")
    await attach_companies(film, [(508, "Regency")])
    await add_event(film=film, summary="a")

    await follow("company", 508)
    await follow("title", film.id)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(film)]


# --- events naming a resolved followed person (M4, D-11's second half) ------------------------


async def test_a_resolved_mention_reaches_the_timeline_without_a_credit(
    entitled_client, make_film, add_event, make_person, follow, name_in_story
):
    # D-11 after M4: an event whose story names a person the user follows, on a film that person
    # holds no credit on. `catalog.film_credit` is empty here, so the credit branch cannot see
    # this film at all — the event branch is the only thing that can put it on the timeline.
    uncredited = await make_film(slug="uncredited", title="Uncredited", release_date=None)
    unrelated = await make_film(slug="unrelated", title="Unrelated", release_date=None)
    await make_person(id=525, name="C. Nolan")
    named = await add_event(
        film=uncredited,
        summary="names them",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/a"},),
    )
    await add_event(
        film=unrelated,
        summary="names nobody",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/b"},),
    )
    await name_in_story(named, person_id=525, path="accepted")

    await follow("person", 525)

    body = (await entitled_client.get("/me/timeline")).json()
    assert [i["film_ref"] for i in body["items"]] == [ref(uncredited)]
    assert body["total"] == 1


async def test_a_muted_film_leaves_the_timeline(
    entitled_client, session, make_film, add_event, follow
):
    """D-45 as amended in M8: "not interested in this film" silences it everywhere, so the
    timeline drops its events beside the calendar and the alerts. The follow is untouched —
    un-muting restores the film on every surface at once (D-40)."""
    muted = await make_film(slug="muted", title="Muted", release_date=None)
    kept = await make_film(slug="kept", title="Kept", release_date=None)
    for film in (muted, kept):
        await add_event(film=film, summary="a beat", created_at=datetime(2026, 6, 3, tzinfo=UTC))
        await follow("title", str(film.id))

    body = (await entitled_client.get("/me/timeline")).json()
    assert sorted(i["film_ref"] for i in body["items"]) == sorted([ref(kept), ref(muted)])

    session.add(WatchlistDismissal(user_id=entitled_client.user.id, film_id=muted.id))
    await session.commit()

    body = (await entitled_client.get("/me/timeline")).json()
    assert [i["film_ref"] for i in body["items"]] == [ref(kept)]


async def test_a_muted_film_drops_a_mention_only_event_too(
    entitled_client, session, make_film, add_event, make_person, follow, name_in_story
):
    """The case the mute would otherwise miss. D-11's second half reaches this event through
    the *person*, not the film — so without the exclusion inside the builder, a muted film
    would keep leaking onto the timeline through every story that names somebody on it."""
    uncredited = await make_film(slug="uncredited", title="Uncredited", release_date=None)
    await make_person(id=525, name="C. Nolan")
    named = await add_event(
        film=uncredited,
        summary="names them",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/a"},),
    )
    await name_in_story(named, person_id=525, path="accepted")
    await follow("person", 525)

    assert (await entitled_client.get("/me/timeline")).json()["total"] == 1

    session.add(WatchlistDismissal(user_id=entitled_client.user.id, film_id=uncredited.id))
    await session.commit()

    assert (await entitled_client.get("/me/timeline")).json()["total"] == 0


@pytest.mark.parametrize(
    ("path", "person_id", "reaches"),
    [
        pytest.param("accepted", 525, True, id="accepted"),
        pytest.param("tiebreak", 525, True, id="tiebreak the resolve stage decided"),
        pytest.param("tiebreak", None, False, id="tiebreak nobody named"),
        pytest.param("unlinked", 525, False, id="unlinked"),
        pytest.param("not_in_tmdb", None, False, id="not_in_tmdb"),
    ],
)
async def test_only_a_resolved_path_matches_a_person_follow(
    entitled_client,
    make_film,
    add_event,
    make_person,
    follow,
    name_in_story,
    path,
    person_id,
    reaches,
):
    # D-25: `unlinked` and `not_in_tmdb` mentions never match a follow. `tiebreak` does, because
    # a mention the resolve stage decided inside the ambiguous band keeps its route and names a
    # person (D-22); an undecided one carries `person_id` NULL and matches nothing.
    #
    # The `unlinked` row is given a person id it would never be written with, so that the rule
    # under test is the *path* and not the null — the one arrangement that tells the two apart.
    film = await make_film(slug="uncredited", title="Uncredited", release_date=None)
    await make_person(id=525, name="C. Nolan")
    event = await add_event(
        film=film,
        summary="a mention",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/a"},),
    )
    await name_in_story(event, person_id=person_id, path=path)

    await follow("person", 525)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == ([ref(film)] if reaches else [])


async def test_a_mention_ships_its_own_event_and_not_the_films_others(
    entitled_client, make_film, add_event, make_person, follow, name_in_story
):
    # The scope the event branch has to keep: a person named in one story about a film they are
    # not credited on makes *that event* timeline-worthy, not everything that film did that day.
    film = await make_film(slug="uncredited", title="Uncredited", release_date=None)
    await make_person(id=525, name="C. Nolan")
    named = await add_event(
        film=film,
        event_type="casting",
        summary="names them",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/a"},),
    )
    await add_event(
        film=film,
        event_type="trailer",
        summary="names nobody",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/b"},),
    )
    await name_in_story(named, person_id=525, path="accepted")

    await follow("person", 525)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["event_count"] for i in items] == [1]
    assert [e["summary"] for i in items for e in i["events"]] == ["names them"]


async def test_a_followed_film_still_ships_every_event_of_its_day(
    entitled_client, make_film, add_event, follow
):
    # The mirror of the test above, and what the OR must not cost: a film that matched on the
    # *film* branch ships its whole day, mentions or no mentions.
    film = await make_film(slug="followed", title="Followed")
    for event_type in ("casting", "trailer"):
        await add_event(
            film=film,
            event_type=event_type,
            summary=f"a {event_type}",
            created_at=datetime(2026, 6, 3, tzinfo=UTC),
            sources=({"url": f"https://deadline.com/{event_type}"},),
        )

    await follow("title", film.id)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["event_count"] for i in items] == [2]


async def test_a_mention_adds_its_day_to_the_timeline(
    entitled_client, make_film, add_event, make_person, follow, name_in_story
):
    # The event scope reaches the day count and the day window, not only the rows inside them: a
    # day whose only qualifying thing was a story naming a followed person is a page of the
    # timeline, and a day on that same film where nobody followed was named is not.
    followed = await make_film(slug="followed", title="Followed")
    uncredited = await make_film(slug="uncredited", title="Uncredited", release_date=None)
    await make_person(id=525, name="C. Nolan")
    await add_event(film=followed, summary="mine", created_at=datetime(2026, 6, 1, tzinfo=UTC))
    named = await add_event(
        film=uncredited,
        summary="names them",
        created_at=datetime(2026, 6, 2, tzinfo=UTC),
        sources=({"url": "https://deadline.com/a"},),
    )
    await add_event(
        film=uncredited,
        summary="names nobody",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/b"},),
    )
    await name_in_story(named, person_id=525, path="accepted")

    await follow("title", followed.id)
    await follow("person", 525)

    body = (await entitled_client.get("/me/timeline?limit=1")).json()
    assert body["total"] == 2
    assert [i["day"] for i in body["items"]] == ["2026-06-02"]

    page_two = (await entitled_client.get("/me/timeline?limit=1&offset=1")).json()
    assert [i["day"] for i in page_two["items"]] == ["2026-06-01"]


async def test_a_mention_of_somebody_elses_followed_person_does_not_reach_this_user(
    entitled_client, make_film, add_event, make_person, follow, name_in_story
):
    # The event branch is scoped to its own user's follows, like every other branch.
    film = await make_film(slug="uncredited", title="Uncredited", release_date=None)
    await make_person(id=525, name="C. Nolan")
    await make_person(id=1032, name="M. Scorsese")
    event = await add_event(
        film=film,
        summary="names the other one",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/a"},),
    )
    await name_in_story(event, person_id=1032, path="accepted")

    await follow("person", 525)

    assert (await entitled_client.get("/me/timeline")).json()["items"] == []


# --- same shape as the feed ------------------------------------------------------------------


async def test_timeline_row_is_identical_to_the_feed_row_for_the_same_film(
    client, entitled_client, make_film, add_event, follow
):
    # D-12: the timeline is the grouped feed filtered, not a second DTO. The client swaps one
    # for the other on `/` once `me` resolves, so any divergence shows up as a broken page.
    film = await make_film(slug="film-2026", title="A Film")
    await add_event(
        film=film,
        event_type="trailer",
        summary="a summary",
        created_at=datetime(2026, 6, 3, 12, tzinfo=UTC),
        sources=({"url": "https://deadline.com/x", "title": "A story"},),
    )

    await follow("title", film.id)

    feed = (await client.get("/feed/grouped")).json()
    timeline = (await entitled_client.get("/me/timeline")).json()
    assert timeline == feed


async def test_timeline_hides_the_same_events_the_feed_does(
    entitled_client, make_film, add_event, follow
):
    # The visibility rules are the feed's, unchanged: no summary, no slug, hidden status.
    followed = await make_film(slug="followed", title="Followed")
    await add_event(film=followed, summary=None)

    await follow("title", followed.id)

    assert (await entitled_client.get("/me/timeline")).json()["items"] == []


# --- pagination ------------------------------------------------------------------------------


async def test_timeline_paginates_by_day_within_the_filter(
    entitled_client, make_film, add_event, follow
):
    # `total` and the window count the days the *filtered* set has, not the feed's — a day on
    # which only unfollowed films moved is not a page of the timeline.
    mine = await make_film(slug="mine", title="Mine")
    theirs = await make_film(slug="theirs", title="Theirs")
    for day in (1, 2, 3):
        await add_event(film=mine, summary="m", created_at=datetime(2026, 6, day, tzinfo=UTC))
    await add_event(film=theirs, summary="t", created_at=datetime(2026, 6, 4, tzinfo=UTC))

    await follow("title", mine.id)

    body = (await entitled_client.get("/me/timeline?limit=2")).json()
    assert body["total"] == 3
    assert [i["day"] for i in body["items"]] == ["2026-06-03", "2026-06-02"]

    page_two = (await entitled_client.get("/me/timeline?limit=2&offset=2")).json()
    assert [i["day"] for i in page_two["items"]] == ["2026-06-01"]


async def test_timeline_rejects_out_of_range_pagination(entitled_client):
    assert (await entitled_client.get("/me/timeline?limit=0")).status_code == 422
    assert (await entitled_client.get("/me/timeline?limit=101")).status_code == 422
    assert (await entitled_client.get("/me/timeline?offset=-1")).status_code == 422


# --- one user's follows are not another's ----------------------------------------------------


async def test_the_timeline_is_scoped_to_its_own_user(
    entitled_client, session, make_user, make_film, add_event, follow
):
    from upmovies.app.models import Follow

    other_user = await make_user(email="other@example.com")
    mine = await make_film(slug="mine", title="Mine")
    theirs = await make_film(slug="theirs", title="Theirs")
    await add_event(film=mine, summary="m")
    await add_event(film=theirs, summary="t")

    await follow("title", mine.id)
    session.add(
        Follow(
            user_id=other_user.id,
            entity_type="title",
            entity_id=str(theirs.id),
            source="manual",
        )
    )
    await session.commit()

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(mine)]
