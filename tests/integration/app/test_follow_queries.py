"""`app.follow_queries.followed_film_ids` (D-11) run *outside* a request.

`tests/integration/routers/test_timeline.py` covers what the filter selects; this file covers
the property the route can never show — that it is a standalone query builder. NEU-1379's notify
pass hands the same SELECT to a batch query from `pipeline_run`, where there is no request, no
enclosing `catalog.film`, and one exception ends the pass for every user at once.
"""

from datetime import date

import pytest
from sqlalchemy import select

from upmovies.app.follow_queries import followed_film_ids
from upmovies.app.models import Follow, User

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
