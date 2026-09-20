"""`app.follow_queries`' builders run *outside* a request — D-11's timeline filters, and M8's
coverage queries beside them.

`tests/integration/routers/test_timeline.py` covers what the filter selects; this file covers
the property the route can never show — that it is a standalone query builder. NEU-1379's notify
pass hands the same SELECT to a batch query from `pipeline_run`, where there is no request, no
enclosing `catalog.film`, and one exception ends the pass for every user at once.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from upmovies.app.follow_queries import (
    covered_by_any_user_clause,
    covered_film_ids,
    covering_follows,
    events_naming_followed_people,
    followed_film_ids,
    watchlist_film_ids,
)
from upmovies.app.models import Follow, User, WatchlistDismissal
from upmovies.catalog.models import Film

TODAY = date(2026, 9, 17)
EXCLUDED = frozenset({"Released", "Canceled"})


def _filter(user_id):
    return followed_film_ids(user_id=user_id, today=TODAY, excluded_statuses=EXCLUDED)


@pytest.fixture
async def user(make_user):
    return await make_user(email="batch@example.com")


async def test_the_filter_executes_on_its_own(session, user, make_film):
    """No enclosing query at all — the shape `correlate(None)` protects."""
    followed = await make_film(slug="followed", title="Followed")
    await make_film(slug="other", title="Other")
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(followed.id), source="manual")
    )
    await session.commit()

    rows = (await session.execute(_filter(user.id))).scalars().all()
    assert list(rows) == [followed.id]


async def test_the_filter_composes_into_a_query_whose_from_is_app_user(session, user, make_film):
    """The notify pass's shape: select users, asking per user whether their follows reach
    anything. `catalog.film` appears only inside the subquery."""
    film = await make_film(slug="followed", title="Followed")
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()

    with_follows = select(User.email).where(User.id == user.id, _filter(user.id).exists())
    assert (await session.execute(with_follows)).scalars().all() == ["batch@example.com"]


async def test_a_non_numeric_entity_id_is_skipped_rather_than_failing_the_query(
    session, user, make_film
):
    """`entity_id` is polymorphic text and only the routes' request models normalise it, so a
    person follow written straight through `follow_service` could hold something that is not an
    integer. That must not take the timeline — or a whole notify pass — down with it.

    A real seed-grade credit has to exist for this to bite: with `catalog.film_credit` empty the
    person branch short-circuits and the cast is never evaluated, so the bad row goes unnoticed.
    """
    from upmovies.catalog.models import FilmCredit, Person

    credited = await make_film(slug="credited", title="Credited", release_date=None)
    followed = await make_film(slug="followed", title="Followed")
    session.add(Person(id=525, name="A Director"))
    await session.flush()
    session.add(
        FilmCredit(
            credit_id="c-1",
            film_id=credited.id,
            person_id=525,
            credit_type="crew",
            job="Director",
            department="Directing",
        )
    )
    session.add(
        Follow(user_id=user.id, entity_type="person", entity_id="nm0000233", source="manual")
    )
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(followed.id), source="manual")
    )
    await session.commit()

    rows = (await session.execute(_filter(user.id))).scalars().all()
    assert list(rows) == [followed.id]


async def test_the_event_filter_executes_on_its_own(
    session, user, make_film, make_person, add_event
):
    """`events_naming_followed_people` is a builder on the same terms as the film filter — the
    notify pass (NEU-1379) asks both questions of every user at once, out of any request."""
    from upmovies.news.models import EventStory, StoryPerson

    film = await make_film(slug="uncredited", title="Uncredited")
    await make_person(id=525, name="C. Nolan")
    named = await add_event(
        film=film, summary="names them", sources=({"url": "https://deadline.com/a"},)
    )
    plain = await add_event(
        film=film, summary="names nobody", sources=({"url": "https://deadline.com/b"},)
    )
    for event, path in ((named, "accepted"), (plain, "unlinked")):
        story_id = await session.scalar(
            select(EventStory.story_id).where(EventStory.event_id == event.id)
        )
        session.add(
            StoryPerson(
                story_id=story_id,
                person_id=525,
                name_as_written="C. Nolan",
                path=path,
                prompt_version="1",
            )
        )
    session.add(Follow(user_id=user.id, entity_type="person", entity_id="525", source="manual"))
    await session.commit()

    rows = (await session.execute(events_naming_followed_people(user.id))).scalars().all()
    assert list(rows) == [named.id]


# --- M8: what a follow covers for alerts (D-42, D-43, D-45) ---------------------------------

MAX_AGE_DAYS = 200
"""`PROVIDER_POLL_MAX_AGE_DAYS`' default, pinned here: the alert window rides on it, and a
boundary test whose boundary moves with the environment is not a boundary test."""


def _covered(user_id, **overrides):
    kwargs = {
        "user_id": user_id,
        "today": TODAY,
        "excluded_statuses": EXCLUDED,
        "max_age_days": MAX_AGE_DAYS,
    }
    return covered_film_ids(**{**kwargs, **overrides})


async def _follow(session, user, entity_type: str, entity_id: str, coverage: str = "lead"):
    session.add(
        Follow(
            user_id=user.id,
            entity_type=entity_type,
            entity_id=entity_id,
            source="manual",
            coverage=coverage,
        )
    )
    await session.commit()


async def _ids(session, stmt) -> set:
    return set((await session.execute(stmt)).scalars().all())


@pytest.mark.parametrize(
    ("credit", "at_lead", "at_all"),
    [
        ({"credit_type": "crew", "job": "Director", "department": "Directing"}, True, True),
        ({"credit_type": "crew", "job": "Screenplay", "department": "Writing"}, False, True),
        ({"credit_type": "cast", "credit_order": 0}, True, True),
        ({"credit_type": "cast", "credit_order": 3}, False, True),
        ({"credit_type": "cast", "credit_order": 5}, False, False),
        ({"credit_type": "cast", "credit_order": None}, False, False),
    ],
)
async def test_the_coverage_tier_decides_which_credits_alert(
    session, user, make_film, credit, at_lead, at_all
):
    """D-43's two tiers against the three cuts in the codebase: `lead` is director-or-top-3,
    `all` is seed grade (director, writer, top-5), and a 6th-billed or unbilled credit is
    outside both. `credit_order = None` is TMDB's long tail and must read as "unbilled", not as
    slot 0."""
    from tests.fixtures.catalog import add_credit

    film = await make_film(slug="covered", title="Covered")
    await add_credit(session, film, 525, **credit)
    await _follow(session, user, "person", "525")

    assert (film.id in await _ids(session, _covered(user.id))) is at_lead

    follow = await session.get(Follow, (user.id, "person", "525"))
    assert follow is not None
    follow.coverage = "all"
    await session.commit()

    assert (film.id in await _ids(session, _covered(user.id))) is at_all


@pytest.mark.parametrize(
    ("release_date", "status", "covered"),
    [
        (TODAY - timedelta(days=MAX_AGE_DAYS), "In Production", True),
        (TODAY - timedelta(days=MAX_AGE_DAYS + 1), "In Production", False),
        (None, "In Production", True),
        (TODAY + timedelta(days=30), "Canceled", False),
        (TODAY - timedelta(days=1), "Released", False),
    ],
)
async def test_the_alert_window_bounds_an_indirect_follow(
    session, user, make_film, release_date, status, covered
):
    """The window is inclusive at its far end — a film exactly `max_age_days` old is still
    covered, one a day older is not, and an undated one always is — while an excluded status
    drops the film whatever its date says.

    The last case is the one to read twice: `Released` is in the default
    `TMDB_EXCLUDED_STATUSES`, so the relaxed date bound does **not** on its own keep a film
    covered through an indirect follow once TMDB marks it out (see `alert_window_clause`). A
    title follow is the affordance that does."""
    from tests.fixtures.catalog import add_credit

    film = await make_film(slug="windowed", title="Windowed", release_date=release_date)
    film.status = status
    await session.commit()
    await add_credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await _follow(session, user, "person", "525")

    assert (film.id in await _ids(session, _covered(user.id))) is covered


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

    watchlist = watchlist_film_ids(
        user_id=user.id,
        today=TODAY,
        excluded_statuses=EXCLUDED,
        max_age_days=MAX_AGE_DAYS,
    )
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
            covering_follows(
                user_id=user.id,
                today=TODAY,
                excluded_statuses=EXCLUDED,
                max_age_days=MAX_AGE_DAYS,
            )
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

    clause = covered_by_any_user_clause(
        today=TODAY, excluded_statuses=EXCLUDED, max_age_days=MAX_AGE_DAYS
    )
    polled = select(Film.id).where(clause)
    assert await _ids(session, polled) == {film.id}

    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
    await session.commit()
    assert await _ids(session, polled) == {film.id}

    session.add(WatchlistDismissal(user_id=other.id, film_id=film.id))
    await session.commit()
    assert await _ids(session, polled) == set()


async def test_a_muted_film_leaves_the_timeline_filter(session, user, make_film):
    """D-45 as amended: the exclusion is inside the D-11 builder, so every consumer honours it
    without a rule of its own."""
    film = await make_film(slug="muted", title="Muted")
    await _follow(session, user, "title", str(film.id))
    assert await _ids(session, _filter(user.id)) == {film.id}

    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
    await session.commit()
    assert await _ids(session, _filter(user.id)) == set()


async def test_a_muted_film_leaves_the_mention_filter_too(
    session, user, make_film, make_person, add_event
):
    """The case the mute would otherwise miss: an event that only *names* a followed person on
    a muted film is still an event about that film."""
    from upmovies.news.models import EventStory, StoryPerson

    film = await make_film(slug="uncredited", title="Uncredited")
    await make_person(id=525, name="C. Nolan")
    event = await add_event(
        film=film, summary="names them", sources=({"url": "https://deadline.com/c"},)
    )
    story_id = await session.scalar(
        select(EventStory.story_id).where(EventStory.event_id == event.id)
    )
    session.add(
        StoryPerson(
            story_id=story_id,
            person_id=525,
            name_as_written="C. Nolan",
            path="accepted",
            prompt_version="1",
        )
    )
    await _follow(session, user, "person", "525")

    assert await _ids(session, events_naming_followed_people(user.id)) == {event.id}

    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
    await session.commit()
    assert await _ids(session, events_naming_followed_people(user.id)) == set()
