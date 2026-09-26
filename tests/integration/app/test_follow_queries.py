"""`app.follow_queries`' builders run *outside* a request — the timeline's two filters, and
the poll set's clause beside them.

`tests/integration/routers/test_timeline.py` covers what the filters select through the route;
this file covers the rule in detail and the property the route can never show — that each is a
standalone query builder. NEU-1379's notify pass hands the same SELECTs to a batch query from
`pipeline_run`, where there is no request, no enclosing `catalog.film` and no `news.event`, and
one exception ends the pass for every user at once.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from upmovies.app.follow_queries import (
    STORY_ATTACH_MENTION_TYPES,
    STORY_DETACH_MENTION_TYPES,
    STORY_PERSON_ATTACH_MENTION_TYPES,
    STORY_PERSON_DETACH_MENTION_TYPES,
    entity_attachment_event_ids,
    entity_event_ids,
    first_association_clause,
    first_association_event_ids,
    follow_attribution_pairs,
    follow_last_activity,
    title_follow_film_ids,
    title_followed_by_any_user_clause,
)
from upmovies.app.models import Follow, User
from upmovies.catalog.models import (
    Collection,
    Film,
    FilmCompanyChange,
    FilmFieldChange,
    FilmProductionCompany,
    ProductionCompany,
)
from upmovies.news.models import EventStory, Story, StoryEntity, StoryPerson
from upmovies.news.subject_key import (
    collection_subject_token,
    company_subject_token,
    normalize_name,
)

TODAY = date(2026, 9, 17)
EXCLUDED = frozenset({"Released", "Canceled"})
"""`TMDB_EXCLUDED_STATUSES`' default. No builder in this module reads it any more — a title
follow reaches its film in any status (EF-14) and an entity follow reaches events, not films —
and it is kept here because the alert window's own status term is a different constant,
`Canceled` alone (D-46), and the two are easy to confuse."""

DIRECTOR = 525
"""One followed person throughout, so a card's `subject_key` and a follow's `entity_id` are
obviously about the same human."""
DIRECTOR_NAME = "Céline  Sciamma"
"""Deliberately not already normalized — an accent and a double space — so every test that
matches this person by name goes through `sql_normalized_name` rather than past it."""
COMPANY = 508
COLLECTION = 726871

OLDER = datetime(2026, 9, 1, 12, tzinfo=UTC)
NEWER = datetime(2026, 9, 18, 9, tzinfo=UTC)
"""Two instants `follow_last_activity`'s assertions can read a `max` between."""


@pytest.fixture
async def user(make_user):
    return await make_user(email="batch@example.com")


async def _follow(session, user, entity_type: str, entity_id: str, source: str = "manual"):
    session.add(
        Follow(
            user_id=user.id,
            entity_type=entity_type,
            entity_id=entity_id,
            source=source,
        )
    )
    await session.commit()


async def _ids(session, stmt) -> set:
    return set((await session.execute(stmt)).scalars().all())


def _titles(user_id):
    return title_follow_film_ids(user_id)


def _events(user_id, **kwargs):
    return entity_attachment_event_ids(user_id, **kwargs)


async def _attach_story(session, event, *, url: str) -> Story:
    """A second (or first) story on an existing card — what a second outlet reporting the same
    casting produces (`add_event(sources=...)` only writes stories at creation)."""
    story = Story(source="Deadline", url=url, title="Story title")
    session.add(story)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=story.id))
    await session.commit()
    return story


async def _mention(
    session,
    event,
    *,
    person_id: int | None = DIRECTOR,
    path: str = "accepted",
    event_type: str | None = "casting",
    story: Story | None = None,
    name: str = DIRECTOR_NAME,
) -> None:
    """The resolver's own row (D-24) on one of `event`'s stories, with the extraction-time
    `event_type` the first-association rule reads out of `features`."""
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


# --- title_follow_film_ids (EF-3, EF-14) -----------------------------------------------------


async def test_the_title_filter_executes_on_its_own(session, user, make_film):
    """No enclosing query at all — the shape `correlate(None)` protects."""
    followed = await make_film(slug="followed", title="Followed")
    await make_film(slug="other", title="Other")
    await _follow(session, user, "title", str(followed.id))

    assert await _ids(session, _titles(user.id)) == {followed.id}


async def test_the_title_filter_composes_into_a_query_whose_from_is_app_user(
    session, user, make_film
):
    """The notify pass's shape: select users, asking per user whether their follows reach
    anything. `catalog.film` appears only inside the subquery."""
    film = await make_film(slug="followed", title="Followed")
    await _follow(session, user, "title", str(film.id))

    with_follows = select(User.email).where(User.id == user.id, _titles(user.id).exists())
    assert (await session.execute(with_follows)).scalars().all() == ["batch@example.com"]


@pytest.mark.parametrize(
    ("status", "release_date"),
    [
        pytest.param("Planned", None, id="undated"),
        pytest.param("In Production", TODAY + timedelta(days=30), id="upcoming"),
        pytest.param("Released", TODAY - timedelta(days=1), id="just released"),
        pytest.param("Released", TODAY - timedelta(days=365 * 12), id="ancient"),
        pytest.param("Canceled", None, id="canceled"),
    ],
)
async def test_a_title_follow_reaches_its_film_in_any_status_and_at_any_age(
    session, user, make_film, status, release_date
):
    """EF-14: the user asked for that film. Every bound the old D-11 builder carried here — the
    in-play cut, the excluded statuses, the age ceiling — is gone, and these are the rows that
    used to fall outside them."""
    film = await make_film(slug="asked-for", title="Asked For", release_date=release_date)
    film.status = status
    await session.commit()
    await _follow(session, user, "title", str(film.id))

    assert await _ids(session, _titles(user.id)) == {film.id}


@pytest.mark.parametrize("entity_type", ["person", "company", "franchise"])
async def test_an_entity_follow_puts_no_film_in_the_title_filter(
    session, user, make_film, make_collection, attach_companies, entity_type
):
    """The cutover, in one assertion (EF-3). All three follows reach this film under the old
    rule — a credit, a production-company row, the collection — and none of them puts it here
    now: they reach *events*, through `entity_attachment_event_ids`."""
    from tests.fixtures.catalog import add_credit

    await make_collection(id=COLLECTION, name="A Franchise")
    film = await make_film(slug="theirs", title="Theirs", collection_id=COLLECTION)
    await add_credit(session, film, DIRECTOR, credit_type="crew", job="Director")
    await attach_companies(film, [(COMPANY, "A Studio")])
    entity_id = {"person": DIRECTOR, "company": COMPANY, "franchise": COLLECTION}[entity_type]
    await _follow(session, user, entity_type, str(entity_id))

    assert await _ids(session, _titles(user.id)) == set()


async def test_unfollowing_is_the_only_way_a_film_leaves_the_title_filter(session, user, make_film):
    """EF-14: nothing subtracts from this set. The mute that used to (D-45) went with the
    watchlist it corrected, and the correction to a list of films you named is to unname one."""
    film = await make_film(slug="followed", title="Followed")
    await _follow(session, user, "title", str(film.id))
    assert await _ids(session, _titles(user.id)) == {film.id}

    await session.execute(
        sa_delete(Follow).where(
            Follow.user_id == user.id,
            Follow.entity_type == "title",
            Follow.entity_id == str(film.id),
        )
    )
    await session.commit()

    assert await _ids(session, _titles(user.id)) == set()


async def test_a_non_uuid_entity_id_is_skipped_rather_than_failing_the_query(
    session, user, make_film
):
    """`entity_id` is polymorphic text and only the routes' request models normalise it, so a
    title follow written straight through `follow_service` could hold something that is not a
    UUID. That must not take the timeline — or a whole notify pass — down with it."""
    followed = await make_film(slug="followed", title="Followed")
    await _follow(session, user, "title", "tt0816692")
    await _follow(session, user, "title", str(followed.id))

    assert await _ids(session, _titles(user.id)) == {followed.id}


# --- entity_attachment_event_ids: the person branch (EF-3, D-1437.3) -------------------------


@pytest.fixture
async def director(session, make_person):
    person = await make_person(id=DIRECTOR, name=DIRECTOR_NAME)
    return person


@pytest.fixture
def attach_card(session, add_event, director):
    """A catalog-sourced credit card naming the followed director, as the sweep writes one:
    `subject_key` carries the *normalized* name and nothing else identifies the person."""

    async def _card(film, *, event_type: str = "casting", name: str = DIRECTOR_NAME, **kwargs):
        kwargs.setdefault("subject_key", [normalize_name(name)])
        kwargs.setdefault("provenance", "catalog")
        kwargs.setdefault("confidence", "rumored")
        return await add_event(film=film, event_type=event_type, **kwargs)

    return _card


@pytest.mark.parametrize("event_type", ["casting", "crew_attached", "credit_removed"])
async def test_a_followed_persons_attach_and_detach_cards_are_selected_by_name(
    session, user, make_film, attach_card, event_type
):
    """The three cards a person attaching to or leaving a film can be. A detach card names the
    departing person exactly as an attach card names the arriving one — the sweep's removal
    path writes `subject_key` from the same `normalize_name` — so the three are one test."""
    film = await make_film(slug="theirs", title="Theirs")
    card = await attach_card(film, event_type=event_type)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _ids(session, _events(user.id)) == {card.id}


@pytest.mark.parametrize("event_type", ["release_date", "trailer", "announced", "now_available"])
async def test_another_beat_on_the_films_day_is_not_selected(
    session, user, make_film, attach_card, add_event, event_type
):
    """The whole point of the cutover (EF-3): a person follow delivers the attachment and not
    the film. The other beat is given the same `subject_key` on purpose, so what excludes it is
    the event type and not a missing name."""
    film = await make_film(slug="theirs", title="Theirs")
    card = await attach_card(film)
    await attach_card(film, event_type=event_type)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _ids(session, _events(user.id)) == {card.id}


async def test_a_superseded_attach_card_is_not_selected(session, user, make_film, attach_card):
    """D-2: the detach card that corrected it is selected instead, and it names the same
    person. A superseded card is still *rendered* wherever it is linked from (ADR-0017); what
    it is not is a beat to deliver."""
    film = await make_film(slug="theirs", title="Theirs")
    withdrawn = await attach_card(film, status="superseded")
    correction = await attach_card(film, event_type="credit_removed")
    withdrawn.superseded_by = correction.id
    await session.commit()
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _ids(session, _events(user.id)) == {correction.id}


async def test_unfollowing_the_person_is_the_only_way_their_card_stops_being_selected(
    session, user, make_film, attach_card
):
    """EF-14: nothing subtracts from the event builder either. These are cards about a person
    that happen to name a film, so a film-level exclusion never belonged here."""
    film = await make_film(slug="attached", title="Attached")
    await attach_card(film)
    await _follow(session, user, "person", str(DIRECTOR))
    assert await _ids(session, _events(user.id)) != set()

    await session.execute(
        sa_delete(Follow).where(
            Follow.user_id == user.id,
            Follow.entity_type == "person",
            Follow.entity_id == str(DIRECTOR),
        )
    )
    await session.commit()

    assert await _ids(session, _events(user.id)) == set()


@pytest.mark.parametrize(
    "as_carded",
    [
        pytest.param("céline  sciamma", id="already normalized"),
        pytest.param("CÉLINE SCIAMMA", id="upper case"),
        pytest.param("Céline Sciamma", id="non-breaking space"),
        pytest.param("  Céline   Sciamma  ", id="padded and double spaced"),
        pytest.param("Céline Sciamma", id="decomposed accent"),
    ],
)
async def test_a_name_that_normalizes_to_the_same_key_still_matches(
    session, user, make_film, add_event, director, as_carded
):
    """The name match is the normalization on both sides, not string equality: the card is
    written under whatever the carding path folded, and `catalog.person.name` is TMDB's
    spelling. These are the pairs that must still meet."""
    film = await make_film(slug="theirs", title="Theirs")
    card = await add_event(film=film, event_type="casting", subject_key=[normalize_name(as_carded)])
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _ids(session, _events(user.id)) == {card.id}


async def test_the_full_fold_divergence_is_a_known_miss(
    session, user, make_film, make_person, add_event
):
    """The accepted loss documented on `sql_normalized_name` and pinned for parity in
    `tests/integration/news/test_sql_normalized_name.py`: `casefold()` turns `ß` into `ss` and
    Postgres's `lower()` does not, so this card is written under a key the name branch cannot
    reach. Pinned here as well because this is where it costs something — a follower's beat.

    The story-backed branch still reaches such a person, because it matches on `person_id`."""
    await make_person(id=770, name="Kaiserstraße Müller")
    film = await make_film(slug="theirs", title="Theirs")
    await add_event(
        film=film, event_type="casting", subject_key=[normalize_name("Kaiserstraße Müller")]
    )
    await _follow(session, user, "person", "770")

    assert await _ids(session, _events(user.id)) == set()


async def test_a_card_naming_somebody_elses_followed_person_does_not_reach_this_user(
    session, user, make_user, make_film, attach_card
):
    """Every branch is scoped to its own user's follows."""
    other = await make_user(email="other@example.com")
    film = await make_film(slug="theirs", title="Theirs")
    await attach_card(film)
    await _follow(session, other, "person", str(DIRECTOR))

    assert await _ids(session, _events(user.id)) == set()


# --- entity_attachment_event_ids: the studio and franchise branches (D-1437.4) ---------------


@pytest.mark.parametrize(
    ("entity_type", "entity_id", "token", "event_types"),
    [
        pytest.param(
            "company",
            COMPANY,
            company_subject_token(COMPANY),
            ("company_attached", "company_removed"),
            id="company",
        ),
        pytest.param(
            "franchise",
            COLLECTION,
            collection_subject_token(COLLECTION),
            ("collection_attached", "collection_removed"),
            id="franchise",
        ),
    ],
)
async def test_an_id_token_selects_the_organisations_beats_and_nothing_else(
    session, user, make_film, add_event, entity_type, entity_id, token, event_types
):
    """The exact half of the delivery rule: companies and collections are matched on the id
    token their cards carry, which is why these two branches lose nothing where the name branch
    can. The `casting` card carrying the same token is the control — the token is not what
    admits a card, the token *on one of these types* is."""
    film = await make_film(slug="theirs", title="Theirs")
    cards = {
        event_type: await add_event(film=film, event_type=event_type, subject_key=[token])
        for event_type in event_types
    }
    await add_event(film=film, event_type="casting", subject_key=[token])
    await add_event(film=film, event_type=event_types[0], subject_key=["company:999999"])
    await _follow(session, user, entity_type, str(entity_id))

    assert await _ids(session, _events(user.id)) == {card.id for card in cards.values()}


async def test_a_title_follow_reaches_those_cards_through_the_film_term(
    session, user, make_film, add_event
):
    """The two halves of the clause do not overlap by accident: a studio card on a film the
    user follows *by title* is theirs through `title_follow_film_ids`, and the event builder —
    which knows nothing about title follows — selects nothing for them."""
    film = await make_film(slug="followed", title="Followed")
    card = await add_event(
        film=film, event_type="company_attached", subject_key=[company_subject_token(COMPANY)]
    )
    await _follow(session, user, "title", str(film.id))

    assert await _ids(session, _titles(user.id)) == {film.id}
    assert await _ids(session, _events(user.id)) == set()
    assert card.film_id == film.id


# --- entity_attachment_event_ids: the canceled branch (D-1437.6, EF-6) -----------------------


@pytest.fixture
def canceled_card(add_event):
    """The cancellation card as NEU-1435 writes one: catalog-sourced, `confirmed`, and with no
    `subject_key` at all — which is why its branch reads the film's current attachments."""

    async def _card(film):
        return await add_event(film=film, event_type="canceled", provenance="catalog")

    return _card


@pytest.mark.parametrize(
    "credit",
    [
        pytest.param({"credit_type": "cast", "credit_order": 39}, id="40th-billed cast"),
        pytest.param(
            {"credit_type": "crew", "job": "Third Assistant Director", "department": "Directing"},
            id="third-unit crew",
        ),
    ],
)
async def test_canceled_reaches_a_person_follower_at_any_credit(
    session, user, make_film, canceled_card, credit
):
    """EF-6 read at query time: "currently attached" is any credit at all, because a follow is
    binary and a film being called off is news to everyone who worked on it."""
    from tests.fixtures.catalog import add_credit

    film = await make_film(slug="called-off", title="Called Off")
    await add_credit(session, film, DIRECTOR, **credit)
    card = await canceled_card(film)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _ids(session, _events(user.id)) == {card.id}


async def test_canceled_reaches_a_company_and_a_franchise_follower(
    session, user, make_user, make_film, make_collection, attach_companies, canceled_card
):
    """The card carries no `subject_key` at all (NEU-1435), so these two followers are found
    through the film's *current* attachments rather than through a token."""
    franchise_follower = await make_user(email="franchise@example.com")
    await make_collection(id=COLLECTION, name="A Franchise")
    film = await make_film(slug="called-off", title="Called Off", collection_id=COLLECTION)
    await attach_companies(film, [(COMPANY, "A Studio")])
    card = await canceled_card(film)
    await _follow(session, user, "company", str(COMPANY))
    await _follow(session, franchise_follower, "franchise", str(COLLECTION))

    assert await _ids(session, _events(user.id)) == {card.id}
    assert await _ids(session, _events(franchise_follower.id)) == {card.id}


async def test_canceled_does_not_reach_a_person_whose_credit_was_removed(
    session, user, make_film, canceled_card, director
):
    """ "Currently attached" and not "was ever attached": somebody TMDB has taken off the film
    is no longer part of it, and the detach card they *did* get is where they heard that."""
    film = await make_film(slug="called-off", title="Called Off")
    card = await canceled_card(film)
    await _follow(session, user, "person", str(DIRECTOR))

    assert card.event_type == "canceled"
    assert await _ids(session, _events(user.id)) == set()


async def test_canceled_reaches_a_title_follower_through_the_film_term(
    session, user, make_film, canceled_card
):
    film = await make_film(slug="called-off", title="Called Off")
    await canceled_card(film)
    await _follow(session, user, "title", str(film.id))

    assert await _ids(session, _titles(user.id)) == {film.id}
    assert await _ids(session, _events(user.id)) == set()


async def test_the_union_is_distinct(session, user, make_film, attach_companies, canceled_card):
    """One row per event however many branches reach it. A user who follows both the film's
    studio and its director reaches this `canceled` card twice, and the `IN` it feeds needs one
    row per event, not one per reason."""
    from tests.fixtures.catalog import add_credit

    film = await make_film(slug="called-off", title="Called Off")
    await add_credit(session, film, DIRECTOR, credit_type="crew", job="Director")
    await attach_companies(film, [(COMPANY, "A Studio")])
    card = await canceled_card(film)
    await _follow(session, user, "person", str(DIRECTOR))
    await _follow(session, user, "company", str(COMPANY))

    rows = (await session.execute(_events(user.id))).scalars().all()
    assert list(rows) == [card.id]


async def test_only_narrows_the_event_builder_to_one_follow(
    session, user, make_film, attach_card, add_event
):
    """NEU-1440's seam: "the newest card that reaches this user through *this* follow". Nothing
    in this ticket passes it, so this test is the only thing holding it."""
    film = await make_film(slug="theirs", title="Theirs")
    person_card = await attach_card(film)
    company_card = await add_event(
        film=film, event_type="company_attached", subject_key=[company_subject_token(COMPANY)]
    )
    await _follow(session, user, "person", str(DIRECTOR))
    await _follow(session, user, "company", str(COMPANY))

    assert await _ids(session, _events(user.id)) == {person_card.id, company_card.id}
    assert await _ids(session, _events(user.id, only=("person", str(DIRECTOR)))) == {person_card.id}
    assert await _ids(session, _events(user.id, only=("company", str(COMPANY)))) == {
        company_card.id
    }
    assert await _ids(session, _events(user.id, only=("title", str(film.id)))) == set()


# --- entity_event_ids: the same rule with nobody following anything (EF-18) ------------------


async def test_the_public_builder_selects_the_entitys_cards_with_no_follow_at_all(
    session, user, make_film, attach_card, add_event
):
    """NEU-1440's entity pages are public: the visitor may have no account, so the id set the
    branches read has to come from the URL rather than from `app.follow`. Same cards, same
    branches — asserted against the per-user builder's answer for a user who follows the same
    two entities, so the two spellings cannot drift."""
    film = await make_film(slug="theirs", title="Theirs")
    person_card = await attach_card(film)
    company_card = await add_event(
        film=film, event_type="company_attached", subject_key=[company_subject_token(COMPANY)]
    )
    await _follow(session, user, "person", str(DIRECTOR))
    await _follow(session, user, "company", str(COMPANY))

    assert await _ids(session, entity_event_ids("person", DIRECTOR)) == {person_card.id}
    assert await _ids(session, entity_event_ids("company", COMPANY)) == {company_card.id}
    assert await _ids(session, entity_event_ids("person", DIRECTOR)) == await _ids(
        session, _events(user.id, only=("person", str(DIRECTOR)))
    )


async def test_the_public_builder_owns_no_title_branch(session, make_film, attach_card):
    """A title follow selects *films*, so there is no "this film's own attachment stream" to
    ask for — and a `title` narrowing must answer with nothing rather than fall through."""
    film = await make_film(slug="theirs", title="Theirs")
    await attach_card(film)

    assert await _ids(session, entity_event_ids("title", 1)) == set()


async def test_the_public_builder_finds_an_entity_nobody_follows(
    session, make_film, attach_card, director
):
    """The point of the swap: an entity page lists its cards whether or not anyone has ever
    followed it. `director` is minted and `attach_card` names them; no `Follow` row exists."""
    film = await make_film(slug="theirs", title="Theirs")
    card = await attach_card(film)

    assert (await session.execute(select(Follow))).scalars().all() == []
    assert await _ids(session, entity_event_ids("person", DIRECTOR)) == {card.id}


# --- follow_last_activity (EF-15) ------------------------------------------------------------


async def test_last_activity_is_one_row_per_follow_across_both_grains(
    session, user, make_film, attach_card, add_event
):
    """The follows page's third sort, batched. Both grains in one result: the title row dates
    to its film's newest beat, the person row to its own card — the trailer that is newer than
    the casting belongs to the film's follower and not to the director's (EF-3)."""
    followed_film = await make_film(slug="mine", title="Mine")
    await add_event(film=followed_film, event_type="casting", created_at=OLDER)
    await add_event(film=followed_film, event_type="trailer", created_at=NEWER)
    theirs = await make_film(slug="theirs", title="Theirs")
    await attach_card(theirs, created_at=OLDER)
    await add_event(film=theirs, event_type="trailer", created_at=NEWER)
    await _follow(session, user, "title", str(followed_film.id))
    await _follow(session, user, "person", str(DIRECTOR))

    rows = dict(
        ((entity_type, entity_id), at)
        for entity_type, entity_id, at in (
            await session.execute(follow_last_activity(user.id))
        ).all()
    )

    assert rows == {
        ("title", str(followed_film.id)): NEWER,
        ("person", str(DIRECTOR)): OLDER,
    }


async def test_a_follow_that_has_delivered_nothing_has_no_row(session, user, make_film):
    """Absent, not NULL. A `GROUP BY` cannot invent a row for a group with no members, and the
    caller reads the absence as "nothing yet" — which is what an outer join would have bought
    at the cost of the aggregate's index."""
    film = await make_film(slug="silent", title="Silent")
    await _follow(session, user, "title", str(film.id))
    await _follow(session, user, "person", str(DIRECTOR))

    assert (await session.execute(follow_last_activity(user.id))).all() == []


async def test_last_activity_narrows_to_one_follow(session, user, make_film, add_event):
    """`only` is what the three single-row follow routes read, so a follow button's answer and
    the list's answer come from one builder."""
    film = await make_film(slug="mine", title="Mine")
    await add_event(film=film, event_type="casting", created_at=OLDER)
    other = await make_film(slug="other", title="Other")
    await add_event(film=other, event_type="casting", created_at=NEWER)
    await _follow(session, user, "title", str(film.id))
    await _follow(session, user, "title", str(other.id))

    rows = (
        await session.execute(follow_last_activity(user.id, only=("title", str(film.id))))
    ).all()

    assert rows == [("title", str(film.id), OLDER)]


async def test_last_activity_is_bounded_by_an_int32_entity_id(session, user, make_film):
    """`_int_id_guard`. An id past int32 passes the digit guard and then fails in the driver as
    it is bound against `Integer` — which used to be a batch pass's problem and became
    `GET /me/follows`' the moment this builder joined that route (`follow_repo._entity_key`)."""
    await make_film(slug="theirs", title="Theirs")
    await _follow(session, user, "person", "9999999999")

    assert (await session.execute(follow_last_activity(user.id))).all() == []


# --- follow_attribution_pairs (DC-6) ---------------------------------------------------------


async def _attribution(session, user_id) -> list[tuple]:
    """Sorted rather than a set, so a pair reached twice would show up twice."""
    return sorted(tuple(row) for row in (await session.execute(follow_attribution_pairs(user_id))))


async def test_attribution_keys_a_person_card_to_the_person(
    session, user, make_film, attach_card, add_event
):
    """The card the follow delivered, and nothing else on its film: a director follow is not a
    subscription to the film's trailer (EF-3), so the trailer has no one to attribute it to."""
    film = await make_film(slug="theirs", title="Theirs")
    card = await attach_card(film, event_type="crew_attached")
    await add_event(film=film, event_type="trailer")
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _attribution(session, user.id) == [("person", str(DIRECTOR), card.id)]


async def test_attribution_keys_a_first_association_to_the_person_it_names(
    session, user, make_film, story_card
):
    """The story-backed branch: no `subject_key` on the card, so the person comes from its
    story's resolved mention (D-1437.5) — and the key comes out with it."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film)
    await _mention(session, card)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _attribution(session, user.id) == [("person", str(DIRECTOR), card.id)]


@pytest.mark.parametrize(
    ("entity_type", "entity_id", "token", "event_type"),
    [
        pytest.param(
            "company", COMPANY, company_subject_token(COMPANY), "company_attached", id="studio"
        ),
        pytest.param(
            "franchise",
            COLLECTION,
            collection_subject_token(COLLECTION),
            "collection_attached",
            id="franchise",
        ),
    ],
)
async def test_attribution_keys_an_organisation_card_to_the_organisation(
    session, user, make_film, add_event, entity_type, entity_id, token, event_type
):
    """The follow graph's word for the type — `franchise`, not the catalog's `collection` — is
    what comes back, because the sender resolves names by the follow it came through."""
    film = await make_film(slug="theirs", title="Theirs")
    card = await add_event(film=film, event_type=event_type, subject_key=[token])
    await _follow(session, user, entity_type, str(entity_id))

    assert await _attribution(session, user.id) == [(entity_type, str(entity_id), card.id)]


async def test_attribution_keys_every_published_beat_on_a_title_followed_film_to_the_title(
    session, user, make_film, add_event
):
    """The title arm: every published beat on the film, whatever its type and however old the
    film (EF-14) — no window and no mute stand between a follow and its reach. A superseded
    card is not a beat to deliver (D-2), on the entity branches' terms."""
    film = await make_film(
        slug="asked-for", title="Asked For", release_date=TODAY - timedelta(days=365 * 12)
    )
    film.status = "Released"
    await session.commit()
    casting = await add_event(film=film, event_type="casting")
    trailer = await add_event(film=film, event_type="trailer")
    await add_event(film=film, event_type="casting", status="superseded")
    await _follow(session, user, "title", str(film.id))

    assert await _attribution(session, user.id) == sorted(
        [("title", str(film.id), casting.id), ("title", str(film.id), trailer.id)]
    )


async def test_a_card_reached_by_a_title_and_a_director_follow_yields_both_rows(
    session, user, make_film, attach_card
):
    """DC-6's own case: the reader asked for the film by name *and* follows its director. The
    mail still names the director, so the entity row must survive beside the title row rather
    than be folded into it."""
    film = await make_film(slug="both", title="Both")
    card = await attach_card(film, event_type="crew_attached")
    await _follow(session, user, "title", str(film.id))
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _attribution(session, user.id) == sorted(
        [("person", str(DIRECTOR), card.id), ("title", str(film.id), card.id)]
    )


async def test_an_event_no_follow_reaches_yields_no_row(
    session, user, make_user, make_film, attach_card, add_event
):
    """Another user's follows reach these cards; this user's reach nothing."""
    other = await make_user(email="other@example.com")
    film = await make_film(slug="theirs", title="Theirs")
    await attach_card(film)
    await add_event(film=film, event_type="trailer")
    await _follow(session, other, "title", str(film.id))
    await _follow(session, other, "person", str(DIRECTOR))
    await _follow(session, user, "company", str(COMPANY))

    assert await _attribution(session, user.id) == []


async def test_attribution_is_one_row_per_follow_and_event(
    session, user, make_film, attach_companies, canceled_card
):
    """A writer-director's `canceled` card comes out of `_canceled_pairs` once per credit; the
    sender names each follow once, so the builder folds that to one row. The studio follow
    reaching the same card is a different reason, and keeps its own row."""
    from tests.fixtures.catalog import add_credit

    film = await make_film(slug="called-off", title="Called Off")
    await add_credit(session, film, DIRECTOR, credit_type="crew", job="Director")
    await add_credit(session, film, DIRECTOR, credit_type="crew", job="Writer")
    await attach_companies(film, [(COMPANY, "A Studio")])
    card = await canceled_card(film)
    await _follow(session, user, "person", str(DIRECTOR))
    await _follow(session, user, "company", str(COMPANY))

    assert await _attribution(session, user.id) == sorted(
        [("company", str(COMPANY), card.id), ("person", str(DIRECTOR), card.id)]
    )


# --- first_association_clause (EF-13, D-1437.5) ----------------------------------------------


@pytest.fixture
def story_card(add_event, director):
    """A story-formed attach card: `provenance = 'story'`, no `subject_key` of its own — the
    people it is about are its stories' resolved mentions, which is the whole input to the
    first-association rule."""

    async def _card(film, *, event_type: str = "casting", url: str = "https://deadline.test/a"):
        return await add_event(
            film=film, event_type=event_type, confidence="rumored", sources=({"url": url},)
        )

    return _card


async def _first(session, user):
    stmt = first_association_event_ids(user_id=user.id)
    return set((await session.execute(stmt)).scalars().all())


async def test_a_story_formed_casting_card_is_a_first_association(
    session, user, make_film, story_card
):
    """The beat the product promises: the trades say somebody has signed on to a film they were
    not on before. No prior card, no credit — so this card, once, for their followers."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film)
    await _mention(session, card)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _first(session, user) == {card.id}
    assert await _ids(session, _events(user.id)) == {card.id}


async def test_a_second_story_on_the_same_card_is_still_one_event(
    session, user, make_film, story_card
):
    """EF-13's headline case: a second outlet reporting the same casting *attaches* to the
    existing card (CONTEXT.md **Attach**), so there is one event and there must be one row."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film)
    await _mention(session, card)
    second = await _attach_story(session, card, url="https://variety.test/b")
    await _mention(session, card, story=second)
    await _follow(session, user, "person", str(DIRECTOR))

    rows = (await session.execute(first_association_event_ids(user_id=user.id))).scalars().all()
    assert list(rows) == [card.id]


async def test_a_split_beats_later_card_is_not_selected(session, user, make_film, story_card):
    """Two cards for one attachment — a split beat, the tolerated failure (CONTEXT.md **Split
    beat**) — must not become two timeline rows: the first association happened once, on the
    earlier card."""
    film = await make_film(slug="uncredited", title="Uncredited")
    first = await story_card(film, url="https://deadline.test/a")
    first.created_at = datetime(2026, 9, 1, tzinfo=UTC)
    later = await story_card(film, url="https://variety.test/b")
    later.created_at = datetime(2026, 9, 2, tzinfo=UTC)
    await session.commit()
    await _mention(session, first)
    await _mention(session, later)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _first(session, user) == {first.id}


async def test_an_earlier_catalog_card_naming_them_blocks_it(
    session, user, make_film, story_card, attach_card
):
    """ "A card names this person" means one thing throughout the module: by name token as well
    as by resolved mention. A sweep-carded attachment last week is a beat the follower has
    already had, so a story about it this week is not their first association."""
    film = await make_film(slug="theirs", title="Theirs")
    earlier = await attach_card(film)
    earlier.created_at = datetime(2026, 9, 1, tzinfo=UTC)
    card = await story_card(film)
    card.created_at = datetime(2026, 9, 2, tzinfo=UTC)
    await session.commit()
    await _mention(session, card)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _first(session, user) == set()
    # The catalog card still reaches them, through the name branch.
    assert await _ids(session, _events(user.id)) == {earlier.id}


async def test_a_baseline_credit_blocks_the_first_association(session, user, make_film, story_card):
    """No change row at all, so nobody ever carded this credit: the person was on the film
    before anyone wrote about it, and the story is a retrospective rather than news."""
    from tests.fixtures.catalog import add_credit

    film = await make_film(slug="credited", title="Credited")
    await add_credit(session, film, DIRECTOR, credit_type="crew", job="Director")
    card = await story_card(film)
    await _mention(session, card)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _first(session, user) == set()


@pytest.mark.parametrize("stamped", [True, False])
async def test_the_cards_own_confirmation_does_not_block_it(
    session, user, make_film, story_card, stamped
):
    """D-1437.5, the carve-out. The story card published first (D-5) and TMDB then confirmed
    the credit, which `news.attachment_confirm` stamped with the card that published it — so the
    credit standing there now is this card's own confirmation, and without the carve-out the
    card would drop off the timeline the morning it arrived.

    Unstamped is the other side: a credit that arrived outside the confirmation window or under
    a hold blocks, and the sweep then raises its own catalog card, which the name branch
    selects. Either way the follower keeps exactly one beat."""
    from tests.fixtures.catalog import add_credit
    from upmovies.catalog.models import FilmCreditChange

    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film)
    await _mention(session, card)
    await add_credit(session, film, DIRECTOR, credit_type="crew", job="Director")
    session.add(
        FilmCreditChange(
            film_id=film.id,
            person_id=DIRECTOR,
            credit_type="crew",
            job="Director",
            change="added",
            carded_by_event_id=card.id if stamped else None,
        )
    )
    await session.commit()
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _first(session, user) == ({card.id} if stamped else set())


@pytest.mark.parametrize(
    ("path", "person_id"),
    [
        pytest.param("accepted", DIRECTOR, id="accepted"),
        pytest.param("tiebreak", DIRECTOR, id="tiebreak the resolve stage decided"),
        pytest.param("tiebreak", None, id="tiebreak nobody named"),
        pytest.param("unlinked", DIRECTOR, id="unlinked"),
        pytest.param("not_in_tmdb", None, id="not_in_tmdb"),
    ],
)
async def test_only_a_resolved_path_names_anybody(
    session, user, make_film, story_card, path, person_id
):
    """D-25, unchanged by this ticket: `unlinked` and `not_in_tmdb` never match a follow. The
    `unlinked` row is given a person id it would never be written with, so what is under test
    is the *path* and not the null."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film)
    await _mention(session, card, person_id=person_id, path=path)
    await _follow(session, user, "person", str(DIRECTOR))

    resolved = path in ("accepted", "tiebreak") and person_id is not None
    assert await _first(session, user) == ({card.id} if resolved else set())


@pytest.mark.parametrize("mention_type", [None, "other", "release_date", "trailer"])
async def test_a_mention_named_in_another_beat_is_not_an_association(
    session, user, make_film, story_card, mention_type
):
    """An interview, a festival piece, a retrospective: the card may be an attach card and the
    person may be resolved, and it is still not news that they have joined anything. The
    mention's own `event_type` is what says so."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film)
    await _mention(session, card, event_type=mention_type)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _first(session, user) == set()


async def test_a_mention_on_a_card_that_is_not_an_attachment_is_not_an_association(
    session, user, make_film, story_card
):
    """Both terms have to hold: an attach-typed mention on a `release_date` card is a story
    about a date that happens to name somebody."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film, event_type="release_date")
    await _mention(session, card)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _first(session, user) == set()
    assert await _ids(session, _events(user.id)) == set()


async def test_the_vocabulary_is_the_union_of_the_three_kinds(session, user, make_film, story_card):
    """M4 filled the detach half of the vocabulary with the two organisation beats and left the
    person half empty (NEU-1446) — the story vocabulary still has no `credit_removed`, so the
    person detach arm is still spelled and dead.

    Pinned as two statements rather than one: the public constants say what the whole
    vocabulary *is*, and the per-kind constants are what the arms actually filter on. A widening
    that reached only one of the two would be exactly the drift this pins."""
    assert STORY_PERSON_ATTACH_MENTION_TYPES == ("casting",)
    assert STORY_PERSON_DETACH_MENTION_TYPES == ()
    assert set(STORY_ATTACH_MENTION_TYPES) == {
        "casting",
        "company_attached",
        "collection_attached",
    }
    assert set(STORY_DETACH_MENTION_TYPES) == {"company_removed", "collection_removed"}

    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film, event_type="credit_removed")
    await _mention(session, card)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _first(session, user) == set()


async def test_the_clause_is_not_reached_by_a_title_narrowing(session, user):
    """`title` is the one type no arm here owns — its follows select films — so the clause must
    come back `None` rather than union nothing or fall through to another kind's arm."""
    assert first_association_clause(user_id=user.id, only=("title", str(uuid4()))) is None
    assert first_association_clause(user_id=user.id, only=("company", str(COMPANY))) is not None
    assert (
        first_association_clause(user_id=user.id, only=("franchise", str(COLLECTION))) is not None
    )


# --- first_association_clause: the organisation arms (EF-13, D-1446.2) ----------------------

ORG_ARMS = [
    pytest.param("company", COMPANY, "company", id="company"),
    pytest.param("franchise", COLLECTION, "collection", id="franchise"),
]
"""The follow's `entity_type`, the TMDB id, and `story_entity.kind` — the three spellings of
one entity (CONTEXT.md **Franchise**), which is most of what these arms have to get right."""


def _attach_type(kind: str) -> str:
    return f"{kind}_attached"


def _detach_type(kind: str) -> str:
    return f"{kind}_removed"


def _token(kind: str, entity_id: int) -> str:
    return (company_subject_token if kind == "company" else collection_subject_token)(entity_id)


_UNSET = object()
"""`None` is a real `event_type` — the extraction pass writes it for a mention it could not tie
to a beat — so the default cannot be spelled `None` without making that case untestable."""


async def _org_mention(
    session,
    event,
    *,
    kind: str,
    entity_id: int | None,
    path: str = "accepted",
    event_type: object = _UNSET,
    story: Story | None = None,
) -> None:
    """A resolved `story_entity` row on one of `event`'s stories. `event_type` defaults to the
    kind's attach beat, which is what makes the mention an attachment claim."""
    story_id = (
        story.id
        if story is not None
        else await session.scalar(
            select(EventStory.story_id).where(EventStory.event_id == event.id)
        )
    )
    session.add(
        StoryEntity(
            story_id=story_id,
            kind=kind,
            entity_id=entity_id,
            name_as_written="Legendary Pictures",
            path=path,
            features={
                "title_mentioned": None,
                "event_type": _attach_type(kind) if event_type is _UNSET else event_type,
            },
            prompt_version="1",
        )
    )
    await session.commit()


async def _hold(session, film, *, kind: str, entity_id: int) -> None:
    """The live attachment each kind reads: a `film_production_company` row, or the film's own
    `collection_id`."""
    if kind == "company":
        session.add(ProductionCompany(id=entity_id, name="Legendary Pictures"))
        await session.flush()
        session.add(FilmProductionCompany(film_id=film.id, company_id=entity_id))
    else:
        session.add(Collection(id=entity_id, name="The Dune Collection"))
        await session.flush()
        film.collection_id = entity_id
    await session.commit()


async def _stamped_change(session, film, *, kind: str, entity_id: int, card, changed_at=NEWER):
    """The change row D-5's stamp writes `carded_by_event_id` onto — the carve-out that keeps a
    story card that published first on the timeline once TMDB confirms it."""
    if kind == "company":
        session.add(
            FilmCompanyChange(
                film_id=film.id,
                company_id=entity_id,
                change="added",
                changed_at=changed_at,
                carded_by_event_id=None if card is None else card.id,
            )
        )
    else:
        session.add(
            FilmFieldChange(
                film_id=film.id,
                field="collection_id",
                old_value=None,
                new_value=entity_id,
                changed_at=changed_at,
                carded_by_event_id=None if card is None else card.id,
            )
        )
    await session.commit()


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
async def test_a_story_formed_attach_card_is_an_organisations_first_association(
    session, user, make_film, story_card, entity_type, entity_id, kind
):
    """EF-13 for studios and franchises: the trades say a studio has boarded a film it was not
    on before, and its followers hear it once."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film, event_type=_attach_type(kind))
    await _org_mention(session, card, kind=kind, entity_id=entity_id)
    await _follow(session, user, entity_type, str(entity_id))

    assert await _first(session, user) == {card.id}
    assert await _ids(session, _events(user.id)) == {card.id}


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
async def test_a_second_story_on_the_same_organisation_card_is_still_one_event(
    session, user, make_film, story_card, entity_type, entity_id, kind
):
    """The headline case, for the two kinds that arrived with M4: a second outlet *attaches* to
    the existing card, so there is one event and there must be one row."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film, event_type=_attach_type(kind))
    await _org_mention(session, card, kind=kind, entity_id=entity_id)
    second = await _attach_story(session, card, url="https://variety.test/b")
    await _org_mention(session, card, kind=kind, entity_id=entity_id, story=second)
    await _follow(session, user, entity_type, str(entity_id))

    rows = (await session.execute(first_association_event_ids(user_id=user.id))).scalars().all()
    assert list(rows) == [card.id]


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
async def test_a_second_organisation_card_on_the_same_film_is_not_selected(
    session, user, make_film, story_card, entity_type, entity_id, kind
):
    """A split beat: two cards for one attachment must not become two timeline rows. The first
    association happened once, on the earlier card."""
    film = await make_film(slug="uncredited", title="Uncredited")
    first = await story_card(film, event_type=_attach_type(kind), url="https://deadline.test/a")
    first.created_at = datetime(2026, 9, 1, tzinfo=UTC)
    later = await story_card(film, event_type=_attach_type(kind), url="https://variety.test/b")
    later.created_at = datetime(2026, 9, 2, tzinfo=UTC)
    await session.commit()
    await _org_mention(session, first, kind=kind, entity_id=entity_id)
    await _org_mention(session, later, kind=kind, entity_id=entity_id)
    await _follow(session, user, entity_type, str(entity_id))

    assert await _first(session, user) == {first.id}


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
async def test_an_earlier_catalog_card_naming_the_organisation_blocks_it(
    session, user, make_film, story_card, add_event, entity_type, entity_id, kind
):
    """ "A card names this entity" means the same two things it means for a person — except
    that for an organisation the catalog half is an *exact* id token rather than a name."""
    film = await make_film(slug="theirs", title="Theirs")
    earlier = await add_event(
        film=film,
        event_type=_attach_type(kind),
        provenance="catalog",
        subject_key=[_token(kind, entity_id)],
    )
    earlier.created_at = datetime(2026, 9, 1, tzinfo=UTC)
    card = await story_card(film, event_type=_attach_type(kind))
    card.created_at = datetime(2026, 9, 2, tzinfo=UTC)
    await session.commit()
    await _org_mention(session, card, kind=kind, entity_id=entity_id)
    await _follow(session, user, entity_type, str(entity_id))

    assert await _first(session, user) == set()
    # The catalog card still reaches them, through the token branch.
    assert await _ids(session, _events(user.id)) == {earlier.id}


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
async def test_a_baseline_attachment_blocks_the_first_association(
    session, user, make_film, story_card, entity_type, entity_id, kind
):
    """No change row at all, so nobody ever carded this attachment: the studio was on the film
    before anyone wrote about it, and the story is a retrospective rather than news."""
    film = await make_film(slug="held", title="Held")
    await _hold(session, film, kind=kind, entity_id=entity_id)
    card = await story_card(film, event_type=_attach_type(kind))
    await _org_mention(session, card, kind=kind, entity_id=entity_id)
    await _follow(session, user, entity_type, str(entity_id))

    assert await _first(session, user) == set()


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
@pytest.mark.parametrize("stamped", [True, False])
async def test_the_organisation_cards_own_confirmation_does_not_block_it(
    session, user, make_film, story_card, entity_type, entity_id, kind, stamped
):
    """D-5's carve-out for the two kinds M4 added. The story card published first and TMDB then
    observed the change, which the sweep's backward stamp attributed to the card that published
    it — so the attachment standing there now is this card's own confirmation.

    Unstamped is the other side: the change blocks, and the sweep raises its own catalog card,
    which the token branch selects. Either way the follower keeps exactly one beat."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film, event_type=_attach_type(kind))
    await _org_mention(session, card, kind=kind, entity_id=entity_id)
    await _hold(session, film, kind=kind, entity_id=entity_id)
    await _stamped_change(
        session, film, kind=kind, entity_id=entity_id, card=card if stamped else None
    )
    await _follow(session, user, entity_type, str(entity_id))

    assert await _first(session, user) == ({card.id} if stamped else set())


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
async def test_a_story_detachment_with_a_prior_attach_card_is_selected(
    session, user, make_film, story_card, add_event, entity_type, entity_id, kind
):
    """The detach arm, live for the first time (M3 left it spelled and empty). A studio's exit
    is news to its followers exactly once per attachment."""
    film = await make_film(slug="left", title="Left")
    attach = await add_event(
        film=film,
        event_type=_attach_type(kind),
        provenance="catalog",
        subject_key=[_token(kind, entity_id)],
    )
    attach.created_at = datetime(2026, 9, 1, tzinfo=UTC)
    card = await story_card(film, event_type=_detach_type(kind))
    card.created_at = datetime(2026, 9, 5, tzinfo=UTC)
    await session.commit()
    await _org_mention(session, card, kind=kind, entity_id=entity_id, event_type=_detach_type(kind))
    await _follow(session, user, entity_type, str(entity_id))

    assert await _first(session, user) == {card.id}


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
async def test_a_detachment_already_reported_since_the_attachment_is_not_selected(
    session, user, make_film, story_card, add_event, entity_type, entity_id, kind
):
    """ "First detachment" is "none since the last attach card", not "none ever" — and a
    detach card published between the two is one the follower has already had."""
    film = await make_film(slug="left", title="Left")
    attach = await add_event(
        film=film,
        event_type=_attach_type(kind),
        provenance="catalog",
        subject_key=[_token(kind, entity_id)],
    )
    attach.created_at = datetime(2026, 9, 1, tzinfo=UTC)
    earlier_detach = await add_event(
        film=film,
        event_type=_detach_type(kind),
        provenance="catalog",
        subject_key=[_token(kind, entity_id)],
    )
    earlier_detach.created_at = datetime(2026, 9, 3, tzinfo=UTC)
    card = await story_card(film, event_type=_detach_type(kind))
    card.created_at = datetime(2026, 9, 5, tzinfo=UTC)
    await session.commit()
    await _org_mention(session, card, kind=kind, entity_id=entity_id, event_type=_detach_type(kind))
    await _follow(session, user, entity_type, str(entity_id))

    assert await _first(session, user) == set()


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
async def test_a_detachment_with_no_attach_card_at_all_is_still_selected(
    session, user, make_film, story_card, entity_type, entity_id, kind
):
    """Where the baseline rule bites the other way round. Almost every studio on almost every
    film is a baseline row with no attach card, so requiring one would silence essentially
    every detachment — and a studio leaving a film we only ever held it on as a baseline is
    genuinely the first detachment we have heard of."""
    film = await make_film(slug="left", title="Left")
    card = await story_card(film, event_type=_detach_type(kind))
    await _org_mention(session, card, kind=kind, entity_id=entity_id, event_type=_detach_type(kind))
    await _follow(session, user, entity_type, str(entity_id))

    assert await _first(session, user) == {card.id}


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
@pytest.mark.parametrize(
    ("path", "resolved_id"),
    [
        pytest.param("accepted", True, id="accepted"),
        pytest.param("tiebreak", True, id="tiebreak the resolve stage decided"),
        pytest.param("tiebreak", False, id="tiebreak nobody named"),
        pytest.param("unlinked", True, id="unlinked"),
        pytest.param("not_in_tmdb", False, id="not_in_tmdb"),
    ],
)
async def test_only_a_resolved_path_names_an_organisation(
    session, user, make_film, story_card, entity_type, entity_id, kind, path, resolved_id
):
    """D-25 over `story_entity`, on `story_person`'s terms exactly: `unlinked` and
    `not_in_tmdb` name nobody, and the `unlinked` row is given an id it would never be written
    with so what is under test is the *path* and not the null."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film, event_type=_attach_type(kind))
    await _org_mention(
        session, card, kind=kind, entity_id=entity_id if resolved_id else None, path=path
    )
    await _follow(session, user, entity_type, str(entity_id))

    selected = path in ("accepted", "tiebreak") and resolved_id
    assert await _first(session, user) == ({card.id} if selected else set())


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
@pytest.mark.parametrize("mention_type", [None, "other", "announced", "release_date"])
async def test_a_possessive_studio_mention_is_not_an_association(
    session, user, make_film, story_card, entity_type, entity_id, kind, mention_type
):
    """ "Legendary's *Dune*" in an interview names the studio and claims nothing. The card may
    be an attach card and the studio may be resolved, and it is still not news that anyone has
    joined anything — the mention's own `event_type` is what says so."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film, event_type=_attach_type(kind))
    await _org_mention(session, card, kind=kind, entity_id=entity_id, event_type=mention_type)
    await _follow(session, user, entity_type, str(entity_id))

    assert await _first(session, user) == set()


@pytest.mark.parametrize(("entity_type", "entity_id", "kind"), ORG_ARMS)
async def test_a_mention_on_an_announced_card_is_not_an_association(
    session, user, make_film, story_card, entity_type, entity_id, kind
):
    """Both terms have to hold: an attach-typed mention on an `announced` card is a story about
    a project that happens to name the studio behind it."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film, event_type="announced")
    await _org_mention(session, card, kind=kind, entity_id=entity_id)
    await _follow(session, user, entity_type, str(entity_id))

    assert await _first(session, user) == set()
    assert await _ids(session, _events(user.id)) == set()


async def test_the_kinds_do_not_cross(session, user, make_film, story_card):
    """`story_entity` holds both organisation kinds in one table, so the `kind` filter is
    load-bearing rather than decorative: a collection mention whose `entity_id` happens to
    equal a followed company's id must not select its card."""
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film, event_type="company_attached")
    await _org_mention(
        session, card, kind="collection", entity_id=COMPANY, event_type="company_attached"
    )
    await _follow(session, user, "company", str(COMPANY))

    assert await _first(session, user) == set()


async def test_the_one_builder_is_what_the_timeline_and_the_notify_pass_reach(monkeypatch):
    """The ticket's own assertion (D-1446.6): `entity_attachment_event_ids` — and so the
    timeline and the digest, which both compose it — reaches the organisation
    arms through `first_association_clause` and not through a second spelling beside it.

    Observed by monkeypatching the builder and reading the narrowing it is called with, because
    the alternative (asserting on rendered SQL) would pass just as well against a copy."""
    import upmovies.app.follow_queries as fq

    calls: list[tuple[str, str] | None] = []
    real = fq.first_association_clause

    def spy(*, user_id, only=None):
        calls.append(only)
        return real(user_id=user_id, only=only)

    monkeypatch.setattr(fq, "first_association_clause", spy)
    user_id = uuid4()

    fq.entity_attachment_event_ids(user_id)
    fq.entity_attachment_event_ids(user_id, only=("company", str(COMPANY)))
    fq.entity_event_ids("franchise", COLLECTION)

    assert calls == [None, ("company", str(COMPANY)), ("franchise", str(COLLECTION))]


async def test_the_notify_pass_reads_the_same_builder(monkeypatch):
    """One step further out. The notify pass reaches the graph through `follow_scope`, which
    composes `entity_attachment_event_ids` by way of `follow_reach` — so the clause it reads is
    this one, organisation arms included."""
    import upmovies.app.follow_queries as fq

    calls: list[tuple[str, str] | None] = []
    real = fq.first_association_clause

    def spy(*, user_id, only=None):
        calls.append(only)
        return real(user_id=user_id, only=only)

    monkeypatch.setattr(fq, "first_association_clause", spy)
    user_id = uuid4()

    fq.follow_scope(user_id)

    assert calls == [None]


# --- title_followed_by_any_user_clause: the poll set's rule 2 (D-1414.3, EF-14) --------------

MAX_AGE_DAYS = 365
"""`PROVIDER_POLL_MAX_AGE_DAYS`' default, pinned here: the alert window rides on it, and a
boundary test whose boundary moves with the environment is not a boundary test. No builder in
this module takes it any more — it is the *poll's* own theatrical window that still does — and
the cases below use it to prove exactly that."""


def _polled():
    return select(Film.id).where(title_followed_by_any_user_clause())


async def test_a_film_anybody_follows_by_title_is_polled(session, user, make_user, make_film):
    followed = await make_film(slug="followed", title="Followed")
    other_film = await make_film(slug="other", title="Other")
    other = await make_user(email="other@example.com")
    await _follow(session, other, "title", str(followed.id))

    assert await _ids(session, _polled()) == {followed.id}
    assert other_film.id not in await _ids(session, _polled())


@pytest.mark.parametrize(
    ("release_date", "status"),
    [
        pytest.param(TODAY + timedelta(days=30), "Post Production", id="upcoming"),
        pytest.param(TODAY - timedelta(days=1), "Released", id="just released"),
        pytest.param(TODAY - timedelta(days=MAX_AGE_DAYS + 1), "Released", id="past the window"),
        pytest.param(TODAY - timedelta(days=MAX_AGE_DAYS * 5), "Released", id="long gone"),
        pytest.param(TODAY + timedelta(days=30), "Canceled", id="canceled"),
        pytest.param(None, None, id="undated"),
    ],
)
async def test_a_title_follow_is_polled_in_any_state_and_at_any_age(
    session, user, make_film, release_date, status
):
    """The clause carries no window and no status term, because a title follow carries none
    (EF-14). The `past the window` and `long gone` cases are the ones that changed: under
    `covered_by_any_user_clause` a title follow was already unbounded, and what this pins is
    that collapsing the four branches to one did not quietly acquire the bound the other three
    needed."""
    film = await make_film(slug="aged", title="Aged", release_date=release_date)
    film.status = status
    await session.commit()
    await _follow(session, user, "title", str(film.id))

    assert await _ids(session, _polled()) == {film.id}


@pytest.mark.parametrize("entity_type", ["person", "company", "franchise"])
async def test_an_entity_follow_polls_nothing(
    session, user, make_film, make_collection, attach_companies, entity_type
):
    """The narrowing EF-14 intends, pinned. `covered_by_any_user_clause` polled a film reached
    only through a followed person, studio or franchise; an entity follow now delivers that
    entity's attachment cards and never the film's other beats (EF-3), so a `now_available` or
    `trailer` card for such a film would reach nobody and buying it would be work for no
    reader.

    The three attachments are real, as in `test_an_entity_follow_puts_no_film_in_the_title_filter`
    next door: what is pinned is that being attached is not being waited on."""
    from tests.fixtures.catalog import add_credit

    await make_collection(id=COLLECTION, name="A Franchise")
    film = await make_film(
        slug="indirect",
        title="Indirect",
        collection_id=COLLECTION,
        release_date=TODAY + timedelta(days=30),
    )
    await add_credit(session, film, DIRECTOR, credit_type="crew", job="Director")
    await attach_companies(film, [(COMPANY, "A Studio")])
    entity_id = {"person": DIRECTOR, "company": COMPANY, "franchise": COLLECTION}[entity_type]
    await _follow(session, user, entity_type, str(entity_id))

    assert await _ids(session, _polled()) == set()


async def test_one_user_unfollowing_leaves_another_users_poll_standing(
    session, user, make_user, make_film
):
    """The clause asks the whole table, so a film two people follow keeps its poll when one of
    them lets go — and loses it only when the last follow does."""
    other = await make_user(email="other@example.com")
    film = await make_film(slug="polled", title="Polled")
    await _follow(session, user, "title", str(film.id))
    await _follow(session, other, "title", str(film.id))
    assert await _ids(session, _polled()) == {film.id}

    await session.execute(sa_delete(Follow).where(Follow.user_id == user.id))
    await session.commit()
    assert await _ids(session, _polled()) == {film.id}

    await session.execute(sa_delete(Follow).where(Follow.user_id == other.id))
    await session.commit()
    assert await _ids(session, _polled()) == set()


async def test_a_non_uuid_title_follow_does_not_fail_the_poll_set(session, user, make_film):
    """The shape guard, asked of the whole table rather than one user: one malformed row must
    not abort a statement the entire provider poll is built from."""
    film = await make_film(slug="followed", title="Followed")
    await _follow(session, user, "title", "tt0816692")
    await _follow(session, user, "title", str(film.id))

    assert await _ids(session, _polled()) == {film.id}


# --- followed_people (D-49, D-50, EF-2) ------------------------------------------------------


async def test_followed_people_names_every_person_follow(session, user, make_user):
    """The set the credit history and the sweep both read. It asks the whole table rather than
    one user's rows, and it does not filter on entitlement — D-40 keeps a lapsed user's
    follows, and the poll set does not filter either.

    Every *person* follow is in it since EF-1: there is no tier left to be outside. The
    `entity_type` filter is all that is left, and the company row proves it still runs — its
    `entity_id` is as numeric as a person id, so without the filter it would arrive as one."""
    from upmovies.app.follow_queries import followed_people

    other = await make_user(email="other@example.com")
    await _follow(session, user, "person", "525")
    await _follow(session, user, "person", "526")
    await _follow(session, other, "person", "527")
    await _follow(session, user, "company", "528")

    assert await _ids(session, followed_people()) == {525, 526, 527}


async def test_followed_people_skips_a_non_numeric_entity_id(session, user):
    """The same shape guard every builder in the module carries: `entity_id` is polymorphic
    text, and a bad row must be skipped rather than abort a statement that runs for the whole
    ingest."""
    from upmovies.app.follow_queries import followed_people

    await _follow(session, user, "person", "nm0000233")
    await _follow(session, user, "person", "525")

    assert await _ids(session, followed_people()) == {525}


async def test_a_non_seed_credit_reaches_the_event_builder(session, user, make_film, attach_card):
    """`covering_follows` and `covered_by_any_user_clause` used to read the same
    `_coverage_credit_clause` beside the timeline, and the poll set reading something the
    alerts did not is the failure that kept them in one builder. Both are gone (EF-14), and
    what survives of the rule is this: a person follow has no credit cut at all, so an
    11th-billed role reaches its follower's cards exactly as a director's does."""
    film = await make_film(slug="minor", title="Minor", release_date=None)
    await attach_card(film)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _ids(session, _events(user.id)) != set()


# --- followed_companies / followed_franchises (EF-4, D-1436.5) -------------------------------


async def test_followed_companies_names_every_company_follow(session, user, make_user):
    """The set the admission exception reads (D-1436.2). Whole-table, no user and no
    entitlement filter, on `followed_people`'s reasoning — and the person and franchise rows
    beside it prove the `entity_type` filter still runs, since their ids are as numeric as a
    company's."""
    from upmovies.app.follow_queries import followed_companies

    other = await make_user(email="other@example.com")
    lapsed = await make_user(
        email="lapsed@example.com", entitled_until=datetime(2020, 1, 1, tzinfo=UTC)
    )
    await _follow(session, user, "company", "420")
    await _follow(session, other, "company", "421")
    await _follow(session, lapsed, "company", "422")
    await _follow(session, user, "person", "423")
    await _follow(session, user, "franchise", "424")

    assert await _ids(session, followed_companies()) == {420, 421, 422}


async def test_followed_companies_is_distinct_across_users(session, user, make_user):
    """Two users following the same studio is one id: the caller is asking what the *system*
    records, not who asked for it."""
    from upmovies.app.follow_queries import followed_companies

    other = await make_user(email="other@example.com")
    await _follow(session, user, "company", "420")
    await _follow(session, other, "company", "420")

    assert await _ids(session, followed_companies()) == {420}


async def test_followed_companies_skips_a_non_numeric_entity_id(session, user):
    """The same shape guard every builder in the module carries: a bad row must be skipped
    rather than abort a statement that runs inside an ingest."""
    from upmovies.app.follow_queries import followed_companies

    await _follow(session, user, "company", "co-lucasfilm")
    await _follow(session, user, "company", "420")

    assert await _ids(session, followed_companies()) == {420}


async def test_followed_franchises_names_every_franchise_follow(session, user, make_user):
    """`entity_type` is `franchise` and the ids are TMDB *collection* ids — the glossary's two
    words for one thing (D-1436.4)."""
    from upmovies.app.follow_queries import followed_franchises

    other = await make_user(email="other@example.com")
    lapsed = await make_user(
        email="lapsed@example.com", entitled_until=datetime(2020, 1, 1, tzinfo=UTC)
    )
    await _follow(session, user, "franchise", "726871")
    await _follow(session, other, "franchise", "8091")
    await _follow(session, lapsed, "franchise", "10")
    await _follow(session, user, "company", "726872")
    await _follow(session, user, "title", str(uuid4()))

    assert await _ids(session, followed_franchises()) == {726871, 8091, 10}


async def test_followed_franchises_is_distinct_across_users(session, user, make_user):
    """Two users following the same franchise is one id: the admission path asks what the
    *system* records, and a duplicate would write the history row twice."""
    from upmovies.app.follow_queries import followed_franchises

    other = await make_user(email="other@example.com")
    await _follow(session, user, "franchise", "726871")
    await _follow(session, other, "franchise", "726871")

    assert await _ids(session, followed_franchises()) == {726871}


async def test_followed_franchises_skips_a_non_numeric_entity_id(session, user):
    from upmovies.app.follow_queries import followed_franchises

    await _follow(session, user, "franchise", "dune")
    await _follow(session, user, "franchise", "726871")

    assert await _ids(session, followed_franchises()) == {726871}
