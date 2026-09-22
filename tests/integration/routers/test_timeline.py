"""`GET /me/timeline` (EF-3, D-12): `/feed/grouped` filtered by the user's follows, behind the
entitlement gate (D-39).

The filter is two builders OR-ed by the grouped feed: a **title** follow selects its film, and
a person, studio or franchise follow selects the *cards* in which that entity attaches to or
detaches from a film, plus that film's cancellation. So a test about an entity follow has to
create the card, not just the credit — an attached director with nothing carded reaches nothing,
which is the cutover in one sentence.

Nothing here depends on a film's status or age any more (EF-14): the in-play cut that made these
fixtures date-sensitive was the old person branch's, and it is gone. The cards the sweep writes
are created the way it writes them — `provenance='catalog'`, `confidence='rumored'`, a
normalized name or an id token in `subject_key` — because that is what the delivery half
matches on.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from tests.fixtures.public import ref
from upmovies.app.models import WatchlistDismissal
from upmovies.news.models import EventStory, Story, StoryPerson
from upmovies.news.subject_key import (
    collection_subject_token,
    company_subject_token,
    normalize_name,
)

DIRECTOR = 525
DIRECTOR_NAME = "C. Nolan"


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

    `features` carries the extraction-time `event_type` — the beat the person was named in
    connection with — which is half of EF-13's first-association rule: a mention typed
    `casting` on an attach card is an association, and the same mention typed anything else is
    an interview.

    Takes the event rather than the story because that is the direction the timeline reads in,
    and `add_event(sources=...)` is what puts a story there in the first place. `story` names
    one of a card's several stories, for the second-outlet case.
    """

    async def _name(
        event,
        *,
        person_id: int | None,
        path: str,
        name: str = "A Person",
        event_type: str | None = "casting",
        story: Story | None = None,
    ) -> None:
        story_id = (
            story.id
            if story is not None
            else await session.scalar(
                select(EventStory.story_id).where(EventStory.event_id == event.id)
            )
        )
        session.add(
            StoryPerson(
                story_id=story_id,
                person_id=person_id,
                name_as_written=name,
                path=path,
                features={"title_mentioned": None, "event_type": event_type},
                prompt_version="1",
            )
        )
        await session.commit()

    return _name


@pytest.fixture
def second_outlet(session):
    """Another story on a card that already exists — what attaching means (CONTEXT.md
    **Attach**), and what must not produce a second row."""

    async def _attach(event, *, url: str) -> Story:
        story = Story(source="Variety", url=url, title="The same casting")
        session.add(story)
        await session.flush()
        session.add(EventStory(event_id=event.id, story_id=story.id))
        await session.commit()
        return story

    return _attach


@pytest.fixture
def catalog_card(add_event):
    """A card as the sweep writes one: catalog-sourced, `rumored` until quarantine clears, and
    identifying its subject in `subject_key`."""

    async def _card(
        film, *, event_type: str, names: tuple[str, ...] = (), tokens: tuple[str, ...] = (), **kw
    ):
        subject_key = [normalize_name(n) for n in names] + list(tokens)
        kw.setdefault("provenance", "catalog")
        kw.setdefault("confidence", "rumored")
        kw.setdefault("created_at", datetime(2026, 6, 3, tzinfo=UTC))
        return await add_event(
            film=film,
            event_type=event_type,
            subject_key=subject_key or None,
            **kw,
        )

    return _card


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


async def test_following_a_director_shows_their_attachment_and_not_the_films_other_beats(
    entitled_client, make_film, make_person, catalog_card, follow
):
    """The ticket's first case, and the cutover in one test (EF-3). Under D-11 this follow
    reached the film and everything on it; now it reaches the card in which the director joined
    it, and the trailer of the same film on the same day is somebody else's business — the
    film's own followers'."""
    await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    theirs = await make_film(slug="theirs", title="Theirs")
    someone_elses = await make_film(slug="not-theirs", title="Not Theirs")
    await catalog_card(theirs, event_type="crew_attached", names=(DIRECTOR_NAME,))
    await catalog_card(theirs, event_type="trailer")
    await catalog_card(someone_elses, event_type="crew_attached", names=("M. Scorsese",))

    await follow("person", DIRECTOR)

    body = (await entitled_client.get("/me/timeline")).json()
    assert [i["film_ref"] for i in body["items"]] == [ref(theirs)]
    assert [i["event_count"] for i in body["items"]] == [1]
    # `event_types` rather than `events`: a catalog-sourced row ships its beats as titles only
    # (NEU-1208), so the row's own aggregate is where the scoped set shows.
    assert [i["event_types"] for i in body["items"]] == [["crew_attached"]]


async def test_a_title_follower_sees_both(
    entitled_client, make_film, make_person, catalog_card, follow
):
    """The other half of the same day: the film's own follower gets the attachment *and* the
    trailer, because a title follow delivers everything about its film."""
    await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    film = await make_film(slug="theirs", title="Theirs")
    await catalog_card(film, event_type="crew_attached", names=(DIRECTOR_NAME,))
    await catalog_card(film, event_type="trailer")

    await follow("title", film.id)

    body = (await entitled_client.get("/me/timeline")).json()
    assert [i["event_count"] for i in body["items"]] == [2]


@pytest.mark.parametrize(
    "out_of_play",
    [
        pytest.param({"release_date": date(2020, 1, 1), "status": "Released"}, id="released"),
        pytest.param({"status": "Canceled", "release_date": None}, id="canceled"),
    ],
)
async def test_a_person_follow_reaches_an_attachment_whatever_the_films_state(
    entitled_client, make_film, make_person, catalog_card, follow, out_of_play
):
    """D-11 scoped a person follow to films *in play*, because a follow that reached films
    would otherwise flood the timeline with a director's back catalogue. An entity follow
    reaches events instead, one per attachment, so there is nothing left to flood with and no
    bound left to apply (EF-3): somebody joining a cancelled film's crew, or a released film's,
    is still news about them."""
    await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    film = await make_film(slug="old", **out_of_play)
    await catalog_card(film, event_type="casting", names=(DIRECTOR_NAME,))

    await follow("person", DIRECTOR)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(film)]


async def test_a_title_follow_reaches_a_released_film(
    entitled_client, make_film, add_event, follow
):
    # Following a title is a request for that specific film, whatever state it is in (EF-14).
    film = await make_film(slug="released", status="Released", release_date=date(2020, 1, 1))
    await add_event(film=film, summary="a")

    await follow("title", film.id)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(film)]


async def test_a_company_follow_reaches_its_attachment_cards_and_nothing_else(
    entitled_client, session, make_film, attach_companies, catalog_card, follow
):
    """The studio half of EF-3, over the id tokens NEU-1433's cards carry. The company is
    attached to both films and only one of them carded it; the release-date beat on the carded
    film is the control.

    The second join row is written directly because `attach_companies` inserts the company
    itself, and this is one company on two films."""
    from upmovies.catalog.models import FilmProductionCompany

    theirs = await make_film(slug="theirs", title="Theirs")
    quiet = await make_film(slug="quiet", title="Quiet")
    await attach_companies(theirs, [(508, "Regency")])
    session.add(FilmProductionCompany(film_id=quiet.id, company_id=508))
    await session.commit()
    await catalog_card(theirs, event_type="company_attached", tokens=(company_subject_token(508),))
    await catalog_card(theirs, event_type="release_date")
    await catalog_card(quiet, event_type="trailer")

    await follow("company", 508)

    body = (await entitled_client.get("/me/timeline")).json()
    assert [i["film_ref"] for i in body["items"]] == [ref(theirs)]
    assert [i["event_types"] for i in body["items"]] == [["company_attached"]]


async def test_a_company_follow_reaches_a_released_films_card(
    entitled_client, make_film, attach_companies, catalog_card, follow
):
    # No window on the event half, as for a person follow: a studio joining a re-release is
    # still the studio's news.
    film = await make_film(slug="released", status="Released", release_date=date(2020, 1, 1))
    await attach_companies(film, [(508, "Regency")])
    await catalog_card(film, event_type="company_attached", tokens=(company_subject_token(508),))

    await follow("company", 508)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(film)]


async def test_a_franchise_follow_reaches_its_collection_cards(
    entitled_client, make_film, add_event, make_collection, catalog_card, follow
):
    """The franchise half: the follow says `franchise`, the token says `collection` (CONTEXT.md
    **Franchise**). A film already sitting in the collection with nothing carded reaches
    nobody — being in a franchise is not news, joining one is."""
    await make_collection(id=10, name="A Collection")
    joined = await make_film(slug="joined", title="Joined", collection_id=10)
    already_in = await make_film(slug="already-in", title="Already In", collection_id=10)
    await catalog_card(
        joined, event_type="collection_attached", tokens=(collection_subject_token(10),)
    )
    await add_event(film=already_in, summary="a trailer", event_type="trailer")

    await follow("franchise", 10)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(joined)]


async def test_a_cancellation_reaches_a_company_follower(
    entitled_client, make_film, attach_companies, catalog_card, follow
):
    """The ticket's fifth case (EF-6). The `canceled` card carries no `subject_key` at all, so
    its branch asks who is *currently attached* to the film — which is why a studio follower
    hears that a film they are on has been called off."""
    film = await make_film(slug="called-off", title="Called Off")
    await attach_companies(film, [(508, "Regency")])
    await catalog_card(film, event_type="canceled", confidence="confirmed")

    await follow("company", 508)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(film)]
    assert [i["event_types"] for i in items] == [["canceled"]]


@pytest.mark.parametrize(
    "credit",
    [
        pytest.param({"credit_order": 39}, id="40th-billed cast"),
        pytest.param({"credit_order": None}, id="unbilled cast"),
    ],
)
async def test_a_cancellation_reaches_a_person_follower_at_any_credit(
    entitled_client, make_film, attach_credits, catalog_card, follow, credit
):
    """A follow is binary (EF-1), so "attached" means any credit — the rows the retired tiers
    declined are exactly the ones to check."""
    film = await make_film(slug="called-off", title="Called Off")
    await attach_credits(film, cast=[{"id": DIRECTOR, "name": DIRECTOR_NAME, **credit}])
    await catalog_card(film, event_type="canceled", confidence="confirmed")

    await follow("person", DIRECTOR)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(film)]


async def test_a_card_matched_by_two_follows_appears_once(
    entitled_client, make_film, attach_companies, catalog_card, follow
):
    """Both halves of the clause reach this card — the film by title, the card by its company
    token — and the union is de-duplicated, so it is one row carrying one event."""
    film = await make_film(slug="both", title="Both")
    await attach_companies(film, [(508, "Regency")])
    await catalog_card(film, event_type="company_attached", tokens=(company_subject_token(508),))

    await follow("company", 508)
    await follow("title", film.id)

    body = (await entitled_client.get("/me/timeline")).json()
    assert [i["film_ref"] for i in body["items"]] == [ref(film)]
    assert [i["event_count"] for i in body["items"]] == [1]


# --- first association: a story mention reaches an entity follower once (EF-13) ---------------


async def test_a_first_association_reaches_the_timeline_without_a_credit(
    entitled_client, make_film, add_event, make_person, follow, name_in_story
):
    """EF-13's whole point: the trades say somebody has signed on to a film they were not on
    before, days before TMDB holds a credit. `catalog.film_credit` is empty here, so nothing
    but the resolved mention can put this card on the timeline — and the story about the
    unrelated film, naming nobody followed, must not."""
    uncredited = await make_film(slug="uncredited", title="Uncredited")
    unrelated = await make_film(slug="unrelated", title="Unrelated")
    await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    named = await add_event(
        film=uncredited,
        event_type="casting",
        summary="names them",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/a"},),
    )
    await add_event(
        film=unrelated,
        event_type="casting",
        summary="names nobody",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/b"},),
    )
    await name_in_story(named, person_id=DIRECTOR, path="accepted")

    await follow("person", DIRECTOR)

    body = (await entitled_client.get("/me/timeline")).json()
    assert [i["film_ref"] for i in body["items"]] == [ref(uncredited)]
    assert body["total"] == 1


async def test_a_second_outlet_on_the_same_card_adds_no_row(
    entitled_client, make_film, add_event, make_person, follow, name_in_story, second_outlet
):
    """The ticket's third case. The second trade to run the casting *attaches* to the card the
    first one formed, so there is one event — and one row, with one event in it. This is the
    row count that would double if the clause selected mentions rather than first
    associations."""
    film = await make_film(slug="uncredited", title="Uncredited")
    await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    card = await add_event(
        film=film,
        event_type="casting",
        summary="the scoop",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/a"},),
    )
    await name_in_story(card, person_id=DIRECTOR, path="accepted")
    story = await second_outlet(card, url="https://variety.com/b")
    await name_in_story(card, person_id=DIRECTOR, path="accepted", story=story)

    await follow("person", DIRECTOR)

    body = (await entitled_client.get("/me/timeline")).json()
    assert body["total"] == 1
    assert [i["event_count"] for i in body["items"]] == [1]


async def test_an_interview_mention_adds_nothing(
    entitled_client, make_film, add_event, make_person, follow, name_in_story
):
    """The ticket's fourth case. The person is resolved and the card is an attach card; what
    the mention is not is an attachment — they were named in connection with another beat, so
    their followers hear nothing."""
    film = await make_film(slug="uncredited", title="Uncredited")
    await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    card = await add_event(
        film=film,
        event_type="casting",
        summary="an interview that mentions the film's casting",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/a"},),
    )
    await name_in_story(card, person_id=DIRECTOR, path="accepted", event_type="other")

    await follow("person", DIRECTOR)

    assert (await entitled_client.get("/me/timeline")).json()["items"] == []


async def test_a_mention_of_somebody_already_credited_adds_nothing(
    entitled_client, make_film, add_event, attach_credits, follow, name_in_story
):
    """The term that does most of the work in production: the director of a film is named in
    every story about it, and none of those namings is news about them joining it."""
    film = await make_film(slug="credited", title="Credited")
    await attach_credits(film, crew=[{"id": DIRECTOR, "name": DIRECTOR_NAME, "job": "Director"}])
    card = await add_event(
        film=film,
        event_type="casting",
        summary="a casting story that names the director too",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/a"},),
    )
    await name_in_story(card, person_id=DIRECTOR, path="accepted")

    await follow("person", DIRECTOR)

    assert (await entitled_client.get("/me/timeline")).json()["items"] == []


async def test_a_muted_film_leaves_the_timeline(
    entitled_client, session, make_film, add_event, follow
):
    """D-45 as amended in M8: "not interested in this film" silences it everywhere, so the
    timeline drops its events beside the calendar and the alerts. The follow is untouched —
    un-muting restores the film on every surface at once (D-40). NEU-1439 takes the whole
    mechanism away."""
    muted = await make_film(slug="muted", title="Muted")
    kept = await make_film(slug="kept", title="Kept")
    for film in (muted, kept):
        await add_event(film=film, summary="a beat", created_at=datetime(2026, 6, 3, tzinfo=UTC))
        await follow("title", str(film.id))

    body = (await entitled_client.get("/me/timeline")).json()
    assert sorted(i["film_ref"] for i in body["items"]) == sorted([ref(kept), ref(muted)])

    session.add(WatchlistDismissal(user_id=entitled_client.user.id, film_id=muted.id))
    await session.commit()

    body = (await entitled_client.get("/me/timeline")).json()
    assert [i["film_ref"] for i in body["items"]] == [ref(kept)]


async def test_a_muted_film_drops_an_attachment_card_too(
    entitled_client, session, make_film, make_person, catalog_card, follow
):
    """The case the mute would otherwise miss: this card reaches the timeline through the
    *person*, not the film, so without the exclusion inside the builder a muted film would keep
    leaking onto the timeline through every attachment anyone made to it."""
    await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    film = await make_film(slug="muted", title="Muted")
    await catalog_card(film, event_type="casting", names=(DIRECTOR_NAME,))
    await follow("person", DIRECTOR)

    assert (await entitled_client.get("/me/timeline")).json()["total"] == 1

    session.add(WatchlistDismissal(user_id=entitled_client.user.id, film_id=film.id))
    await session.commit()

    assert (await entitled_client.get("/me/timeline")).json()["total"] == 0


@pytest.mark.parametrize(
    ("path", "person_id", "reaches"),
    [
        pytest.param("accepted", DIRECTOR, True, id="accepted"),
        pytest.param("tiebreak", DIRECTOR, True, id="tiebreak the resolve stage decided"),
        pytest.param("tiebreak", None, False, id="tiebreak nobody named"),
        pytest.param("unlinked", DIRECTOR, False, id="unlinked"),
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
    film = await make_film(slug="uncredited", title="Uncredited")
    await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    event = await add_event(
        film=film,
        event_type="casting",
        summary="a mention",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        sources=({"url": "https://deadline.com/a"},),
    )
    await name_in_story(event, person_id=person_id, path=path)

    await follow("person", DIRECTOR)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["film_ref"] for i in items] == ([ref(film)] if reaches else [])


async def test_an_attachment_ships_its_own_event_and_not_the_films_others(
    entitled_client, make_film, make_person, add_event, follow, name_in_story
):
    # The scope the event half has to keep: an attachment makes *that event* timeline-worthy,
    # not everything that film did that day. Story-formed cards here, so the day's events are
    # shipped in full and the assertion can read the summaries rather than the beat names.
    await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    film = await make_film(slug="uncredited", title="Uncredited")
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
    await name_in_story(named, person_id=DIRECTOR, path="accepted")

    await follow("person", DIRECTOR)

    items = (await entitled_client.get("/me/timeline")).json()["items"]
    assert [i["event_count"] for i in items] == [1]
    assert [e["summary"] for i in items for e in i["events"]] == ["names them"]


async def test_a_followed_film_still_ships_every_event_of_its_day(
    entitled_client, make_film, add_event, follow
):
    # The mirror of the test above, and what the OR must not cost: a film that matched on the
    # *film* branch ships its whole day, attachments or no attachments.
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


async def test_an_attachment_adds_its_day_to_the_timeline(
    entitled_client, make_film, make_person, add_event, catalog_card, follow
):
    # The event scope reaches the day count and the day window, not only the rows inside them: a
    # day whose only qualifying thing was an attachment of a followed person is a page of the
    # timeline, and a day on that same film where nothing of theirs happened is not.
    await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    followed = await make_film(slug="followed", title="Followed")
    theirs = await make_film(slug="theirs", title="Theirs")
    await add_event(film=followed, summary="mine", created_at=datetime(2026, 6, 1, tzinfo=UTC))
    await catalog_card(
        theirs,
        event_type="casting",
        names=(DIRECTOR_NAME,),
        created_at=datetime(2026, 6, 2, tzinfo=UTC),
    )
    await catalog_card(theirs, event_type="trailer", created_at=datetime(2026, 6, 3, tzinfo=UTC))

    await follow("title", followed.id)
    await follow("person", DIRECTOR)

    body = (await entitled_client.get("/me/timeline?limit=1")).json()
    assert body["total"] == 2
    assert [i["day"] for i in body["items"]] == ["2026-06-02"]

    page_two = (await entitled_client.get("/me/timeline?limit=1&offset=1")).json()
    assert [i["day"] for i in page_two["items"]] == ["2026-06-01"]


async def test_a_card_naming_somebody_elses_followed_person_does_not_reach_this_user(
    entitled_client, make_film, make_person, catalog_card, follow
):
    # The event half is scoped to its own user's follows, like every other branch.
    await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    await make_person(id=1032, name="M. Scorsese")
    film = await make_film(slug="theirs", title="Theirs")
    await catalog_card(film, event_type="casting", names=("M. Scorsese",))

    await follow("person", DIRECTOR)

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
