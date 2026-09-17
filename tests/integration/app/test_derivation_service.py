"""D-13's derivation rule: which films a user's follows put on their watchlist, and what
stops one being put back (`app.services.derivation_service`).

The cut is the rule's own, not the timeline's: **director or top-3 billing**, where D-11's
person follows reach seed grade (director, writer, top-5). A test that only ever used a
director credit would pass under either, so the writer and the 4th-billed cases are what
actually pin it down.

The pass writes through one statement per user, so the exclusions — an item already on the
list, a dismissal on file — are asserted here rather than at the route: they are part of what
the INSERT selects, and a caller cannot reinstate them by checking first.
"""

from datetime import date

import pytest
from sqlalchemy import select

from upmovies.app.models import Follow, WatchlistDismissal, WatchlistItem
from upmovies.app.services import derivation_service, follow_service
from upmovies.catalog.models import FilmCredit, FilmProductionCompany

TODAY = date(2026, 9, 17)
EXCLUDED = frozenset({"Released", "Canceled"})
IN_PLAY = date(2026, 12, 25)


@pytest.fixture
async def user(make_user):
    return await make_user(email="deriver@example.com")


async def _derive(session, user, **overrides) -> int:
    kwargs = {"user_id": user.id, "today": TODAY, "excluded_statuses": EXCLUDED}
    added = await derivation_service.derive_for_user(session, **{**kwargs, **overrides})
    await session.commit()
    return added


async def _items(session, user) -> list[WatchlistItem]:
    rows = await session.execute(
        select(WatchlistItem)
        .where(WatchlistItem.user_id == user.id)
        .order_by(WatchlistItem.film_id)
    )
    return list(rows.scalars().all())


async def _film_ids(session, user) -> set:
    return {item.film_id for item in await _items(session, user)}


async def _credit(session, film, person_id: int, **fields) -> None:
    session.add(
        FilmCredit(
            credit_id=f"c-{film.id}-{person_id}-{fields.get('job') or fields.get('credit_order')}",
            film_id=film.id,
            person_id=person_id,
            **fields,
        )
    )
    await session.flush()


async def _follow_person(session, user, person_id: int) -> None:
    session.add(
        Follow(user_id=user.id, entity_type="person", entity_id=str(person_id), source="manual")
    )
    await session.commit()


# --- the person cut (director or top-3) ----------------------------------------------------


async def test_a_followed_director_derives_their_in_play_film(
    session, user, make_film, make_person
):
    film = await make_film(slug="directed", title="Directed", release_date=IN_PLAY)
    await make_person(id=525, name="A Director")
    await _credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await _follow_person(session, user, 525)

    assert await _derive(session, user) == 1
    (item,) = await _items(session, user)
    assert item.film_id == film.id
    assert item.source == "derived_from_follow"
    # The column's server default, not a value the pass restates (D-14).
    assert item.alert_prefs == ["stream"]


async def test_top_three_billing_derives_and_fourth_does_not(session, user, make_film, make_person):
    """The line D-13 draws and D-11 does not: billing 0-2 is the watchlist's cut, while
    `catalog.seed_grade`'s top-5 keeps 3 and 4 in the *timeline*."""
    third = await make_film(slug="third", title="Third Billed", release_date=IN_PLAY)
    fourth = await make_film(slug="fourth", title="Fourth Billed", release_date=IN_PLAY)
    await make_person(id=287, name="An Actor")
    await _credit(session, third, 287, credit_type="cast", credit_order=2, character="Self")
    await _credit(session, fourth, 287, credit_type="cast", credit_order=3, character="Self")
    await _follow_person(session, user, 287)

    assert await _derive(session, user) == 1
    assert await _film_ids(session, user) == {third.id}


async def test_a_writer_credit_alone_does_not_derive(session, user, make_film, make_person):
    """Writers feed the timeline only (D-13), so a seed-grade credit is not enough here."""
    film = await make_film(slug="written", title="Written", release_date=IN_PLAY)
    await make_person(id=488, name="A Writer")
    await _credit(session, film, 488, credit_type="crew", job="Screenplay", department="Writing")
    await _follow_person(session, user, 488)

    assert await _derive(session, user) == 0
    assert await _items(session, user) == []


async def test_an_unbilled_cast_credit_does_not_derive(session, user, make_film, make_person):
    """`credit_order` NULL is TMDB's long tail, which the `< 3` cut must not admit as 0."""
    film = await make_film(slug="unbilled", title="Unbilled", release_date=IN_PLAY)
    await make_person(id=999, name="An Extra")
    await _credit(session, film, 999, credit_type="cast", credit_order=None, character="Extra")
    await _follow_person(session, user, 999)

    assert await _derive(session, user) == 0


# --- the other three entity types ----------------------------------------------------------


async def test_a_followed_company_derives_its_film(session, user, make_film, make_company):
    film = await make_film(slug="produced", title="Produced", release_date=IN_PLAY)
    await make_company(id=4, name="Paramount")
    session.add(FilmProductionCompany(film_id=film.id, company_id=4))
    session.add(Follow(user_id=user.id, entity_type="company", entity_id="4", source="manual"))
    await session.commit()

    assert await _derive(session, user) == 1
    assert await _film_ids(session, user) == {film.id}


async def test_a_followed_franchise_derives_its_film(session, user, make_film, make_collection):
    await make_collection(id=10, name="A Franchise")
    film = await make_film(slug="sequel", title="Sequel", release_date=IN_PLAY, collection_id=10)
    session.add(Follow(user_id=user.id, entity_type="franchise", entity_id="10", source="manual"))
    await session.commit()

    assert await _derive(session, user) == 1
    assert await _film_ids(session, user) == {film.id}


async def test_a_followed_title_derives_that_film(session, user, make_film):
    film = await make_film(slug="titled", title="Titled", release_date=IN_PLAY)
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()

    assert await _derive(session, user) == 1
    assert await _film_ids(session, user) == {film.id}


# --- in play -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "release_date", "status"),
    [
        ("released", date(2026, 1, 1), "Released"),
        ("canceled", None, "Canceled"),
    ],
)
async def test_a_film_no_longer_in_play_is_not_derived(
    session, user, make_film, slug, release_date, status
):
    """In play qualifies **every** branch here, unlike D-11 where it is the person branch's
    alone: a watchlist exists to say *this is coming*, and a title follow on a film that has
    already come has nothing left to alert on."""
    film = await make_film(slug=slug, title=slug, release_date=release_date, status=status)
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()

    assert await _derive(session, user) == 0


# --- what blocks a derivation --------------------------------------------------------------


async def test_a_dismissal_blocks_the_derivation_permanently(session, user, make_film):
    film = await make_film(slug="dismissed", title="Dismissed", release_date=IN_PLAY)
    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()

    assert await _derive(session, user) == 0
    assert await _items(session, user) == []


async def test_an_item_already_on_the_list_is_left_as_it_is(session, user, make_film):
    """A manual item the follows would also reach keeps its source and its prefs — the pass
    adds, it never rewrites (D-14)."""
    film = await make_film(slug="manual", title="Manual", release_date=IN_PLAY)
    session.add(
        WatchlistItem(
            user_id=user.id, film_id=film.id, source="manual", alert_prefs=["buy", "rent"]
        )
    )
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()

    assert await _derive(session, user) == 0
    (item,) = await _items(session, user)
    assert item.source == "manual"
    assert item.alert_prefs == ["buy", "rent"]


async def test_the_pass_is_idempotent(session, user, make_film):
    film = await make_film(slug="twice", title="Twice", release_date=IN_PLAY)
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()

    assert await _derive(session, user) == 1
    assert await _derive(session, user) == 0
    assert len(await _items(session, user)) == 1


async def test_another_users_follows_derive_nothing_here(session, user, make_user, make_film):
    other = await make_user(email="other@example.com")
    film = await make_film(slug="theirs", title="Theirs", release_date=IN_PLAY)
    session.add(
        Follow(user_id=other.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()

    assert await _derive(session, user) == 0
    assert await _items(session, other) == []


async def test_a_non_numeric_person_follow_does_not_fail_the_pass(
    session, user, make_film, make_person
):
    """The same exposure `follow_queries` carries: `entity_id` is polymorphic text, normalised
    by the routes' request models but not by `follow_service`, and this pass runs over every
    user at once — one bad row must not abort the statement for all of them."""
    film = await make_film(slug="ok", title="OK", release_date=IN_PLAY)
    await make_person(id=525, name="A Director")
    await _credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    session.add(
        Follow(user_id=user.id, entity_type="person", entity_id="not-a-number", source="manual")
    )
    await _follow_person(session, user, 525)

    assert await _derive(session, user) == 1
    assert await _film_ids(session, user) == {film.id}


# --- scoped to one follow (the POST /me/follows half) --------------------------------------


async def test_deriving_for_one_follow_ignores_the_users_other_follows(session, user, make_film):
    """What the follow-creation half asks: the new follow's films, not a full re-derivation.
    The other follow's film is left for the sweep pass, which is the half that is allowed to
    cost a scan over everything the user follows."""
    new = await make_film(slug="new", title="New", release_date=IN_PLAY)
    old = await make_film(slug="old", title="Old", release_date=IN_PLAY)
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(old.id), source="manual")
    )
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(new.id), source="manual")
    )
    await session.commit()

    added = await derivation_service.derive_for_follow(
        session, user_id=user.id, entity_type="title", entity_id=str(new.id)
    )
    await session.commit()

    assert added == 1
    assert await _film_ids(session, user) == {new.id}


async def test_deriving_for_one_follow_resolves_its_own_clock(session, user, make_film):
    """`derive_for_follow` is the request-time entry point and resolves `today` itself.

    The film is dated in the past with a status that is *not* excluded, so only the clock can
    rule it out: a `derive_for_follow` that passed, say, `date.min` would derive it."""
    past = await make_film(
        slug="gone", title="Gone", release_date=date(2020, 1, 1), status="In Production"
    )
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(past.id), source="manual")
    )
    await session.commit()

    added = await derivation_service.derive_for_follow(
        session, user_id=user.id, entity_type="title", entity_id=str(past.id)
    )
    await session.commit()

    assert added == 0


async def test_deriving_for_one_follow_uses_the_configured_statuses(session, user, make_film):
    """The other half of the same resolution: an undated film — in play by date — is still out
    when its status is one `TMDB_EXCLUDED_STATUSES` names."""
    canceled = await make_film(
        slug="called-off", title="Called Off", release_date=None, status="Canceled"
    )
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(canceled.id), source="manual")
    )
    await session.commit()

    added = await derivation_service.derive_for_follow(
        session, user_id=user.id, entity_type="title", entity_id=str(canceled.id)
    )
    await session.commit()

    assert added == 0


async def test_deriving_for_a_company_follow_scopes_to_that_company(
    session, user, make_film, make_company
):
    film = await make_film(slug="studio", title="Studio", release_date=IN_PLAY)
    await make_company(id=4, name="Paramount")
    session.add(FilmProductionCompany(film_id=film.id, company_id=4))
    session.add(Follow(user_id=user.id, entity_type="company", entity_id="4", source="manual"))
    await session.commit()

    added = await derivation_service.derive_for_follow(
        session, user_id=user.id, entity_type="company", entity_id="4"
    )
    await session.commit()

    assert added == 1
    assert await _film_ids(session, user) == {film.id}


async def test_a_person_follow_created_now_is_visible_to_the_derivation(
    session, user, make_film, make_person
):
    """The follow row is flushed, not committed, when the service derives from it — so the
    scoped query has to read it inside the same transaction."""
    film = await make_film(slug="fresh", title="Fresh", release_date=IN_PLAY)
    await make_person(id=525, name="A Director")
    await _credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    session.add(Follow(user_id=user.id, entity_type="person", entity_id="525", source="manual"))
    await session.flush()

    added = await derivation_service.derive_for_follow(
        session, user_id=user.id, entity_type="person", entity_id="525"
    )
    await session.commit()

    assert added == 1
    assert await _film_ids(session, user) == {film.id}


async def test_an_undated_film_is_in_play(session, user, make_film, make_person):
    """`release_date` NULL is in play (`in_play_clause`) — the sweep's whole working set is
    undated films, and they are exactly what a follow is a standing interest in."""
    film = await make_film(slug="undated", title="Undated", release_date=None)
    await make_person(id=525, name="A Director")
    await _credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await _follow_person(session, user, 525)

    assert await _derive(session, user) == 1


async def test_derived_items_survive_the_follow_being_dropped(session, user, make_film):
    """Not this module's behaviour but its consequence, asserted where the rule is: unfollowing
    deletes the follow only (`follow_service.unfollow`), and nothing re-derives or reaps the
    item afterwards. Run through the service rather than deleting the row, so a future
    `unfollow` that reaped derived items would fail here."""
    film = await make_film(slug="kept", title="Kept", release_date=IN_PLAY)
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()
    assert await _derive(session, user) == 1

    await follow_service.unfollow(session, user=user, entity_type="title", entity_id=str(film.id))

    assert await _film_ids(session, user) == {film.id}
    assert await _derive(session, user) == 0


async def test_a_malformed_title_follow_does_not_fail_the_pass(
    session, user, make_film, make_person
):
    """The `title` branch's half of the same exposure, and the one the batch pass makes
    expensive: `entity_id` is cast to UUID there, so a row that is not one would raise for this
    user on every sweep for as long as it existed, not just once."""
    film = await make_film(slug="fine", title="Fine", release_date=IN_PLAY)
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id="not-a-uuid", source="manual")
    )
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()

    assert await _derive(session, user) == 1
    assert await _film_ids(session, user) == {film.id}


async def test_one_follow_derives_every_film_it_reaches(session, user, make_film, make_person):
    """D-13 is plural: a director's *films*, not the first of them. One `INSERT ... SELECT`
    writes them all, and an implementation that derived one at a time would pass every
    single-film test above."""
    first = await make_film(slug="one", title="One", release_date=IN_PLAY)
    second = await make_film(slug="two", title="Two", release_date=None)
    await make_person(id=525, name="A Director")
    await _credit(session, first, 525, credit_type="crew", job="Director", department="Directing")
    await _credit(session, second, 525, credit_type="crew", job="Director", department="Directing")
    await session.commit()

    added = await derivation_service.derive_for_follow(
        session, user_id=user.id, entity_type="person", entity_id="525"
    )
    await session.commit()

    assert added == 0  # the follow itself does not exist yet
    await _follow_person(session, user, 525)

    added = await derivation_service.derive_for_follow(
        session, user_id=user.id, entity_type="person", entity_id="525"
    )
    await session.commit()

    assert added == 2
    assert await _film_ids(session, user) == {first.id, second.id}


async def test_a_failed_derivation_takes_the_follow_with_it(
    session, user, make_film, make_person, monkeypatch
):
    """The trade `follow_service.follow` makes by deriving in the follow's transaction: the
    user retries one action, rather than keeping a follow whose items quietly wait for the next
    sweep. Pinned because the alternative — commit the follow, then derive — is a one-line
    change somebody could make without noticing it changes this."""
    film = await make_film(slug="doomed", title="Doomed", release_date=IN_PLAY)
    await make_person(id=525, name="A Director")
    await _credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await session.commit()

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated derivation failure")

    monkeypatch.setattr(follow_service.derivation_service, "derive_for_follow", boom)

    with pytest.raises(RuntimeError):
        await follow_service.follow(session, user=user, entity_type="person", entity_id="525")
    # A rollback expires every loaded object, so the user is re-read before it is asked for
    # its id again — the alternative is a lazy load with no greenlet to run it in.
    await session.rollback()
    await session.refresh(user)

    assert (await session.execute(select(Follow))).scalars().all() == []
    assert await _items(session, user) == []
