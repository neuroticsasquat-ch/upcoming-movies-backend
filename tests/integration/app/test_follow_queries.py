"""`app.follow_queries`' builders run *outside* a request — the timeline's two filters, and
what is left of M8's coverage queries beside them.

`tests/integration/routers/test_timeline.py` covers what the filters select through the route;
this file covers the rule in detail and the property the route can never show — that each is a
standalone query builder. NEU-1379's notify pass hands the same SELECTs to a batch query from
`pipeline_run`, where there is no request, no enclosing `catalog.film` and no `news.event`, and
one exception ends the pass for every user at once.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from upmovies.app.follow_queries import (
    covered_by_any_user_clause,
    covered_film_ids,
    covering_follows,
    entity_attachment_event_ids,
    first_association_clause,
    title_follow_film_ids,
    watchlist_film_ids,
)
from upmovies.app.models import Follow, User, WatchlistDismissal
from upmovies.catalog.models import Film
from upmovies.news.models import EventStory, Story, StoryPerson
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


async def test_a_muted_title_followed_film_is_absent(session, user, make_film):
    """D-45 as amended: the exclusion is inside the builder, so every consumer honours it
    without a rule of its own. NEU-1439 takes it out with the table."""
    film = await make_film(slug="muted", title="Muted")
    await _follow(session, user, "title", str(film.id))
    assert await _ids(session, _titles(user.id)) == {film.id}

    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
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


async def test_a_card_on_a_muted_film_is_not_selected(session, user, make_film, attach_card):
    """The mute is applied once, at the top of the builder, rather than per branch."""
    film = await make_film(slug="muted", title="Muted")
    await attach_card(film)
    await _follow(session, user, "person", str(DIRECTOR))
    assert await _ids(session, _events(user.id)) != set()

    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
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
    return set((await session.execute(first_association_clause(user_id=user.id))).scalars().all())


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

    rows = (await session.execute(first_association_clause(user_id=user.id))).scalars().all()
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
    the credit, which `news.credit_confirm` stamped with the card that published it — so the
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


async def test_the_detach_arm_selects_nothing_at_m3(session, user, make_film, story_card):
    """`STORY_DETACH_MENTION_TYPES` is empty because the story vocabulary has no detach type,
    so the arm is spelled and dead. Pinned rather than left to be noticed: the day M4 fills the
    constant, this test is the one that has to be rewritten, which is where the rule is."""
    from upmovies.app.follow_queries import STORY_DETACH_MENTION_TYPES

    assert STORY_DETACH_MENTION_TYPES == ()
    film = await make_film(slug="uncredited", title="Uncredited")
    card = await story_card(film, event_type="credit_removed")
    await _mention(session, card)
    await _follow(session, user, "person", str(DIRECTOR))

    assert await _first(session, user) == set()


async def test_the_clause_is_not_reached_by_a_non_person_narrowing(session, user, make_film):
    """`only=("company", …)` has no arm here until M4 extends this builder to `story_entity`,
    and until then it must select nothing rather than fall through to the person arm."""
    assert await _first(session, user) == set()
    assert (
        await _ids(
            session, first_association_clause(user_id=user.id, only=("company", str(COMPANY)))
        )
        == set()
    )


# --- M8: what a follow covers for alerts (D-42, D-43, D-45) ---------------------------------

MAX_AGE_DAYS = 365
"""`PROVIDER_POLL_MAX_AGE_DAYS`' default, pinned here: the alert window rides on it, and a
boundary test whose boundary moves with the environment is not a boundary test."""


def _covered(user_id, **overrides):
    kwargs = {"user_id": user_id, "today": TODAY, "max_age_days": MAX_AGE_DAYS}
    return covered_film_ids(**{**kwargs, **overrides})


_EVERY_CREDIT_SHAPE = [
    {"credit_type": "crew", "job": "Director", "department": "Directing"},
    {"credit_type": "crew", "job": "Screenplay", "department": "Writing"},
    {"credit_type": "cast", "credit_order": 0},
    {"credit_type": "cast", "credit_order": 3},
    {"credit_type": "cast", "credit_order": 5},
    {"credit_type": "cast", "credit_order": None},
    {"credit_type": "crew", "job": "Gaffer", "department": "Lighting"},
]
"""One credit of every shape the three deleted tiers used to sort between.

Kept as a list rather than collapsed to a single case because it is the whole content of EF-2:
the rows at the bottom — a 6th-billed role, an unbilled one, a crew job that is neither
directing nor writing — are exactly the ones `lead` and `major` declined, and a seed-grade
term creeping back into any builder would show up here and nowhere else. `credit_order = None`
is TMDB's long tail and must read as "unbilled", never as slot 0."""


@pytest.mark.parametrize("credit", _EVERY_CREDIT_SHAPE)
async def test_every_credit_of_a_followed_person_alerts(session, user, make_film, credit):
    """EF-1 and EF-2: the follow is binary, so there is no cut between the credits a person
    holds and the ones that reach their follower. This replaces D-43 and D-48's tier table."""
    from tests.fixtures.catalog import add_credit

    film = await make_film(slug="covered", title="Covered")
    await add_credit(session, film, 525, **credit)
    await _follow(session, user, "person", "525")

    assert film.id in await _ids(session, _covered(user.id))


@pytest.mark.parametrize(
    ("release_date", "status", "covered"),
    [
        (TODAY - timedelta(days=MAX_AGE_DAYS), "In Production", True),
        (TODAY - timedelta(days=MAX_AGE_DAYS + 1), "In Production", False),
        (None, "In Production", True),
        (None, None, True),
        (TODAY + timedelta(days=30), "Canceled", False),
        (TODAY - timedelta(days=1), "Released", True),
        (TODAY - timedelta(days=MAX_AGE_DAYS), "Released", True),
        (TODAY - timedelta(days=MAX_AGE_DAYS + 1), "Released", False),
    ],
)
async def test_the_alert_window_bounds_an_indirect_follow(
    session, user, make_film, release_date, status, covered
):
    """The window is inclusive at its far end — a film exactly `max_age_days` old is still
    covered, one a day older is not, and an undated one always is — while `Canceled` drops the
    film whatever its date says.

    The `Released` cases are the ones to read twice (D-46): `Released` is **inside** the window
    and rides the date bound exactly like any other status, because it is the state the
    home-release beats land in. The window's status term is `ALERT_WINDOW_DEAD_STATUSES`, not
    `TMDB_EXCLUDED_STATUSES` — `Released` being in the latter is what this pins as no longer
    relevant here. Only `Canceled` is out."""
    from tests.fixtures.catalog import add_credit

    film = await make_film(slug="windowed", title="Windowed", release_date=release_date)
    film.status = status
    await session.commit()
    await add_credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await _follow(session, user, "person", "525")

    assert (film.id in await _ids(session, _covered(user.id))) is covered


async def test_a_released_film_is_covered_for_alerts_but_reaches_no_timeline(
    session, user, make_film
):
    """The two questions differ on purpose, and this pins the difference rather than assuming
    it. One person follow, one recently-released film: the alert window holds it, because that
    is where the `now_available` beat is about to land — and the timeline's film half holds
    nothing at all now, because a person follow no longer reaches films (EF-3). What it reaches
    is the film's attachment cards, and this film has none."""
    from tests.fixtures.catalog import add_credit

    film = await make_film(
        slug="just-out", title="Just Out", release_date=TODAY - timedelta(days=30)
    )
    film.status = "Released"
    await session.commit()
    await add_credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await _follow(session, user, "person", "525")

    assert await _ids(session, _covered(user.id)) == {film.id}
    assert await _ids(session, _titles(user.id)) == set()
    assert await _ids(session, _events(user.id)) == set()


async def test_a_title_follow_covers_its_film_in_any_state(session, user, make_film):
    """The window is the *indirect* branches'. The user asked for this film by name, and one
    they added the week it came out is exactly the one they are waiting on the home release
    of."""
    film = await make_film(
        slug="old", title="Old", release_date=TODAY - timedelta(days=MAX_AGE_DAYS * 5)
    )
    film.status = "Released"
    await session.commit()
    await _follow(session, user, "title", str(film.id))

    assert await _ids(session, _covered(user.id)) == {film.id}


async def test_the_watchlist_is_the_covered_set_minus_the_mutes(session, user, make_film):
    covered = await make_film(slug="covered", title="Covered")
    muted = await make_film(slug="muted", title="Muted")
    await _follow(session, user, "title", str(covered.id))
    await _follow(session, user, "title", str(muted.id))
    session.add(WatchlistDismissal(user_id=user.id, film_id=muted.id))
    await session.commit()

    watchlist = watchlist_film_ids(user_id=user.id, today=TODAY, max_age_days=MAX_AGE_DAYS)
    assert await _ids(session, _covered(user.id)) == {covered.id, muted.id}
    assert await _ids(session, watchlist) == {covered.id}


async def test_only_narrows_the_graph_to_one_follow(session, user, make_film):
    """What the want/stop service asks: "does anything *else* cover this film?"."""
    film = await make_film(slug="both", title="Both")
    await _follow(session, user, "title", str(film.id))

    assert await _ids(session, _covered(user.id, only=("title", str(film.id)))) == {film.id}
    assert await _ids(session, _covered(user.id, only=("company", "1"))) == set()


async def test_covering_follows_pairs_a_film_with_every_follow_that_reaches_it(
    session, user, make_film
):
    """One pair per (film, follow), and exactly one even when the person holds two qualifying
    credits on the film — a director who is also top-billed is one reason, not two."""
    from tests.fixtures.catalog import add_credit

    film = await make_film(slug="double", title="Double")
    await add_credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await add_credit(session, film, 525, credit_type="cast", credit_order=0)
    await _follow(session, user, "person", "525")
    await _follow(session, user, "title", str(film.id))

    rows = (
        await session.execute(
            covering_follows(user_id=user.id, today=TODAY, max_age_days=MAX_AGE_DAYS)
        )
    ).all()
    assert sorted((row.film_id, row.entity_type) for row in rows) == sorted(
        [(film.id, "person"), (film.id, "title")]
    )


async def test_covered_by_any_user_holds_while_one_uncovering_user_remains(
    session, user, make_user, make_film
):
    """The provider poll's rule 2 (D-1414.3). The mute is per covering user: a film ten people
    follow and one has muted is still owed a poll."""
    other = await make_user(email="other@example.com")
    film = await make_film(slug="polled", title="Polled")
    await _follow(session, user, "title", str(film.id))
    await _follow(session, other, "title", str(film.id))

    clause = covered_by_any_user_clause(today=TODAY, max_age_days=MAX_AGE_DAYS)
    polled = select(Film.id).where(clause)
    assert await _ids(session, polled) == {film.id}

    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
    await session.commit()
    assert await _ids(session, polled) == {film.id}

    session.add(WatchlistDismissal(user_id=other.id, film_id=film.id))
    await session.commit()
    assert await _ids(session, polled) == set()


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


async def test_a_non_seed_credit_reaches_the_other_two_alert_builders(session, user, make_film):
    """`covering_follows` and `covered_by_any_user_clause` used to read the same
    `_coverage_credit_clause`, and the poll set reading something the alerts do not is the
    failure that kept them in one builder — so dropping that clause has to reach all three."""
    from sqlalchemy import select as sa_select

    from tests.fixtures.catalog import add_credit
    from upmovies.app.follow_queries import covered_by_any_user_clause
    from upmovies.catalog.models import Film as FilmModel

    film = await make_film(slug="minor", title="Minor", release_date=None)
    await add_credit(session, film, 525, credit_type="cast", credit_order=11)
    await _follow(session, user, "person", "525")

    pairs = (
        await session.execute(
            covering_follows(user_id=user.id, today=TODAY, max_age_days=MAX_AGE_DAYS)
        )
    ).all()
    assert [(r.film_id, r.entity_type, r.entity_id) for r in pairs] == [(film.id, "person", "525")]

    polled = await _ids(
        session,
        sa_select(FilmModel.id).where(
            covered_by_any_user_clause(today=TODAY, max_age_days=MAX_AGE_DAYS)
        ),
    )
    assert film.id in polled


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
