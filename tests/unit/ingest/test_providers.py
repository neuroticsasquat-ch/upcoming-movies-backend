"""The provider poll's two pure pieces: flattening a region payload into offers, and the
detail line the run reports itself on."""

from upmovies.ingest.providers import ProvidersResult, offers_for_region, providers_detail
from upmovies.ingest.tmdb.schemas import TMDBWatchProviderRegion


def _region(**kwargs) -> TMDBWatchProviderRegion:
    return TMDBWatchProviderRegion.model_validate(kwargs)


def test_offers_flatten_every_monetization_type_in_render_order():
    region = _region(
        link="https://example.test/watch",
        flatrate=[{"provider_id": 8, "provider_name": "Eight"}],
        rent=[{"provider_id": 2, "provider_name": "Two"}],
        buy=[{"provider_id": 3, "provider_name": "Three"}],
    )

    offers = offers_for_region(region)

    assert [(o.provider_id, o.monetization_type) for o in offers] == [
        (8, "flatrate"),
        (2, "rent"),
        (3, "buy"),
    ]


def test_one_provider_on_two_tiers_is_two_offers():
    """A service that both rents and sells a film is two separate beats — `now_available`
    cards per (film, monetization_type) — so they must not collapse into one offer."""
    region = _region(
        rent=[{"provider_id": 2, "provider_name": "Two"}],
        buy=[{"provider_id": 2, "provider_name": "Two"}],
    )

    offers = offers_for_region(region)

    assert [o.monetization_type for o in offers] == ["rent", "buy"]


def test_a_provider_listed_twice_in_one_tier_is_one_offer():
    """Both tables are keyed on (film, region, provider, monetization type), so a duplicate
    inside one list — regional duplicates do occur in the JustWatch data — would reach the
    snapshot rebuild as two rows with the same key, raise, and cost the film its poll."""
    region = _region(
        flatrate=[
            {"provider_id": 8, "provider_name": "Eight"},
            {"provider_id": 8, "provider_name": "Eight"},
        ]
    )

    offers = offers_for_region(region)

    assert [(o.provider_id, o.monetization_type) for o in offers] == [(8, "flatrate")]


def test_a_region_tmdb_holds_nothing_for_flattens_to_no_offers():
    """The ordinary answer for a film nobody carries in the US. It has to read as "no offers"
    — which empties the where-to-watch box — rather than raise or be skipped."""
    assert offers_for_region(None) == []


def test_detail_reports_first_seen_apart_from_offers():
    line = providers_detail(
        ProvidersResult(
            selected=10, polled=9, offers=31, first_seen=2, cards=1, missing=1, failures=0
        )
    )

    assert line == "providers: 9/10 polled, 31 offers, 2 first seen, 1 carded, 1 missing, 0 failed"


def test_detail_reports_cards_apart_from_first_seen():
    """The gap between the two is the churn the product swallows (D-28): a film that moved
    service inserts a ledger row and cards nothing, and the line has to show both numbers for
    that to be readable rather than look like a lost card."""
    line = providers_detail(ProvidersResult(selected=1, polled=1, offers=1, first_seen=1))

    assert "1 first seen, 0 carded" in line


def test_detail_says_so_when_the_poll_gave_up():
    line = providers_detail(
        ProvidersResult(selected=10, polled=3, aborted=True, abort_error="aborted after 10")
    )

    assert line.endswith("providers aborted: aborted after 10")
