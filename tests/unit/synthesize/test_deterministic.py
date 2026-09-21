from datetime import date

import pytest

from upmovies.synthesize.deterministic import (
    DETERMINISTIC_MODEL,
    TEMPLATE_VERSION,
    AvailableOn,
    CompaniesAttached,
    CompaniesDetached,
    CompanyAttached,
    CompanyDetached,
    CreditAttached,
    CreditDetached,
    CreditsAttached,
    CreditsDetached,
    NowAvailable,
    ReleaseDateChanged,
    ReleaseDatesChanged,
    StatusChanged,
    TrailerReleased,
    render_summary,
)


def test_release_date_set_names_the_market_and_the_new_date():
    # A first date is never a slip (D-1403.1): it has nothing to be later than.
    assert render_summary(
        ReleaseDateChanged(region="US", label="wide", new_date=date(2026, 8, 14))
    ) == ("US wide release date set to 14 August 2026.")


def test_release_date_set_does_not_zero_pad_the_day():
    assert render_summary(
        ReleaseDateChanged(region="US", label="wide", new_date=date(2026, 8, 2))
    ) == ("US wide release date set to 2 August 2026.")


def test_home_release_date_uses_the_same_template():
    # D-26 widens the labels, not the phrasing — one template covers all four buckets.
    assert render_summary(
        ReleaseDateChanged(region="US", label="digital", new_date=date(2026, 10, 14))
    ) == ("US digital release date set to 14 October 2026.")


def test_home_release_date_slip_uses_the_same_verb():
    # D-1403.4: one template for every bucket, so a physical slip reads as a wide one does.
    assert render_summary(
        ReleaseDateChanged(
            region="US",
            label="physical",
            new_date=date(2026, 12, 1),
            previous_date=date(2026, 11, 3),
        )
    ) == ("US physical release date slipped from 3 November 2026 to 1 December 2026.")


def test_release_date_slip_names_both_dates():
    # D-1403.1: a strictly later date is a slip, and the verb is the only string that moves.
    assert render_summary(
        ReleaseDateChanged(
            region="US",
            label="limited",
            previous_date=date(2026, 8, 14),
            new_date=date(2026, 10, 2),
        )
    ) == ("US limited release date slipped from 14 August 2026 to 2 October 2026.")


def test_two_markets_moving_together_share_one_body():
    # uq_event_catalog_change permits one catalog event per film/type/timestamp, so a
    # distributor shifting limited and wide at once has to render as one card (NEU-1121).
    assert render_summary(
        ReleaseDatesChanged(
            changes=(
                ReleaseDateChanged(
                    region="US",
                    label="wide",
                    previous_date=date(2027, 12, 17),
                    new_date=date(2028, 1, 15),
                ),
                ReleaseDateChanged(region="GB", label="limited", new_date=date(2028, 1, 8)),
            )
        )
    ) == (
        "US wide release date slipped from 17 December 2027 to 15 January 2028. "
        "GB limited release date set to 8 January 2028."
    )


def test_a_one_market_group_renders_as_the_single_change_does():
    single = ReleaseDateChanged(region="US", label="wide", new_date=date(2026, 8, 14))
    assert render_summary(ReleaseDatesChanged(changes=(single,))) == render_summary(single)


def test_an_earlier_date_is_not_a_slip():
    # D-1403.1 keeps the direction-neutral verb for the earlier case: "moved up" is a US
    # idiom, and the reader has both dates.
    assert render_summary(
        ReleaseDateChanged(
            region="US",
            label="wide",
            previous_date=date(2026, 10, 2),
            new_date=date(2026, 8, 14),
        )
    ) == ("US wide release date moved from 2 October 2026 to 14 August 2026.")


def test_a_mixed_group_flags_only_the_clauses_that_slipped():
    # D-1403.2: "later" is judged per clause, in diff order — a group-level "delayed" would
    # be a lie about the market that moved earlier.
    assert render_summary(
        ReleaseDatesChanged(
            changes=(
                ReleaseDateChanged(
                    region="US",
                    label="wide",
                    previous_date=date(2027, 12, 17),
                    new_date=date(2028, 1, 15),
                ),
                ReleaseDateChanged(
                    region="US",
                    label="digital",
                    previous_date=date(2028, 3, 1),
                    new_date=date(2028, 2, 15),
                ),
                ReleaseDateChanged(region="GB", label="limited", new_date=date(2028, 1, 8)),
            )
        )
    ) == (
        "US wide release date slipped from 17 December 2027 to 15 January 2028. "
        "US digital release date moved from 1 March 2028 to 15 February 2028. "
        "GB limited release date set to 8 January 2028."
    )


def test_a_slip_in_another_region_uses_the_same_verb():
    # D-1403.4: no per-region phrasing.
    assert render_summary(
        ReleaseDateChanged(
            region="GB",
            label="limited",
            previous_date=date(2026, 8, 14),
            new_date=date(2026, 8, 21),
        )
    ) == ("GB limited release date slipped from 14 August 2026 to 21 August 2026.")


def test_an_equal_date_renders_as_moved():
    # D-1403.3: strictly later. The sweep never sends an equal pair, but the renderer stays
    # total rather than guarding — "moved" is at least not a lie.
    assert render_summary(
        ReleaseDateChanged(
            region="US",
            label="wide",
            previous_date=date(2026, 8, 14),
            new_date=date(2026, 8, 14),
        )
    ) == ("US wide release date moved from 14 August 2026 to 14 August 2026.")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("In Production", "Shooting has started."),
        ("Post Production", "Shooting has wrapped."),
        ("Released", "The film has been released."),
        ("Canceled", "The film has been canceled."),
        ("Planned", "The film is now listed as planned."),
        ("Rumored", "The film is now listed as rumored."),
    ],
)
def test_status_change_renders_its_template(status, expected):
    assert render_summary(StatusChanged(new_status=status)) == expected


def test_unknown_status_falls_back_to_naming_it():
    """TMDB can add a status; a catalog event must still get a body rather than crash the
    stage that creates it — an event with no summary row is invisible everywhere (§5.4)."""
    assert render_summary(StatusChanged(new_status="Shelved")) == (
        "The film's production status is now Shelved."
    )


def test_director_attached():
    assert render_summary(CreditAttached(role="director", name="Denis Villeneuve")) == (
        "Denis Villeneuve attached to direct."
    )


def test_writer_attached():
    assert render_summary(CreditAttached(role="writer", name="Jon Spaihts")) == (
        "Jon Spaihts attached to write."
    )


def test_cast_attached_without_a_character():
    assert render_summary(CreditAttached(role="cast", name="Zendaya")) == (
        "Zendaya joins the cast."
    )


def test_cast_attached_with_a_character():
    assert render_summary(CreditAttached(role="cast", name="Zendaya", character="Chani")) == (
        "Zendaya joins the cast as Chani."
    )


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError, match="producer"):
        render_summary(CreditAttached(role="producer", name="Mary Parent"))


def test_sentinel_model_is_not_a_real_model_id():
    """`model` must never be a real model id: pricing keys on (provider, model) and the cost
    ledger must not carry rows for a call that was never made (ADR-0014)."""
    assert DETERMINISTIC_MODEL == "deterministic"
    assert TEMPLATE_VERSION.startswith("deterministic-")


def test_several_cast_attached_in_one_observation_read_as_one_beat():
    """Three performers added between two ingests is one beat, not three cards — so the
    body has to name all of them (NEU-1083)."""
    assert render_summary(
        CreditsAttached(
            credits=(
                CreditAttached(role="cast", name="Timothée Chalamet"),
                CreditAttached(role="cast", name="Zendaya"),
                CreditAttached(role="cast", name="Rebecca Ferguson"),
            )
        )
    ) == ("Timothée Chalamet, Zendaya and Rebecca Ferguson join the cast.")


def test_a_cast_clause_reads_in_billing_order():
    """A burst card can name six performers (D-7), and the order they are named in is the
    only ranking the body carries — so it is TMDB's billing order, not the diff's."""
    assert render_summary(
        CreditsAttached(
            credits=(
                CreditAttached(role="cast", name="Rebecca Ferguson", credit_order=2),
                CreditAttached(role="cast", name="Timothée Chalamet", credit_order=0),
                CreditAttached(role="cast", name="Zendaya", credit_order=1),
            )
        )
    ) == ("Timothée Chalamet, Zendaya and Rebecca Ferguson join the cast.")


def test_an_unbilled_cast_credit_reads_after_the_billed_ones():
    """No `credit_order` means no claim on a position — never a claim on the first one."""
    assert render_summary(
        CreditsAttached(
            credits=(
                CreditAttached(role="cast", name="Unbilled"),
                CreditAttached(role="cast", name="Top Billed", credit_order=0),
            )
        )
    ) == ("Top Billed and Unbilled join the cast.")


def test_two_people_in_one_role_share_a_clause():
    assert render_summary(
        CreditsAttached(
            credits=(
                CreditAttached(role="writer", name="Jon Spaihts"),
                CreditAttached(role="writer", name="Eric Roth"),
            )
        )
    ) == ("Jon Spaihts and Eric Roth attached to write.")


def test_roles_render_one_clause_each_in_seed_grade_order():
    """A director and a writer arriving in one edit is a single `crew_attached` event —
    `uq_event_catalog_change` allows only one catalog event per film per timestamp — so both
    have to fit in one body, strongest attachment first."""
    assert render_summary(
        CreditsAttached(
            credits=(
                CreditAttached(role="writer", name="Jon Spaihts"),
                CreditAttached(role="director", name="Denis Villeneuve"),
            )
        )
    ) == ("Denis Villeneuve attached to direct. Jon Spaihts attached to write.")


def test_one_credit_renders_exactly_as_the_singular_change():
    change = CreditAttached(role="cast", name="Zendaya", character="Chani")

    assert render_summary(CreditsAttached(credits=(change,))) == render_summary(change)


def test_unknown_role_is_rejected_in_a_group_too():
    with pytest.raises(ValueError, match="producer"):
        render_summary(CreditsAttached(credits=(CreditAttached(role="producer", name="M P"),)))


def test_template_version_bumped():
    """Bumped to 8 by the company bodies (EF-5): a summary has to be traceable back to the
    phrasing that produced it, so this moves whenever a template above does."""
    assert TEMPLATE_VERSION == "deterministic-8"


# ── Detachment summary tests (NEU-1200) ──────────────────────────────────


def test_render_detached_director_singular():
    assert (
        render_summary(CreditDetached(role="director", name="Denis Villeneuve"))
        == "Denis Villeneuve is no longer attached to direct."
    )


def test_render_detached_director_plural():
    assert (
        render_summary(
            CreditsDetached(
                credits=(
                    CreditDetached(role="director", name="Phil Lord"),
                    CreditDetached(role="director", name="Chris Miller"),
                )
            )
        )
        == "Phil Lord and Chris Miller are no longer attached to direct."
    )


def test_render_detached_writer_singular():
    assert (
        render_summary(CreditDetached(role="writer", name="Jon Spaihts"))
        == "Jon Spaihts is no longer attached to write."
    )


def test_render_detached_writer_plural():
    assert (
        render_summary(
            CreditsDetached(
                credits=(
                    CreditDetached(role="writer", name="Jon Spaihts"),
                    CreditDetached(role="writer", name="Eric Roth"),
                )
            )
        )
        == "Jon Spaihts and Eric Roth are no longer attached to write."
    )


def test_render_detached_cast_singular():
    assert (
        render_summary(CreditDetached(role="cast", name="Zendaya")) == "Zendaya departs the cast."
    )


def test_render_detached_cast_plural():
    assert (
        render_summary(
            CreditsDetached(
                credits=(
                    CreditDetached(role="cast", name="Timothée Chalamet"),
                    CreditDetached(role="cast", name="Zendaya"),
                )
            )
        )
        == "Timothée Chalamet and Zendaya depart the cast."
    )


def test_render_detached_multi_role_group():
    assert render_summary(
        CreditsDetached(
            credits=(
                CreditDetached(role="director", name="Denis Villeneuve"),
                CreditDetached(role="cast", name="Timothée Chalamet"),
            )
        )
    ) == ("Denis Villeneuve is no longer attached to direct. Timothée Chalamet departs the cast.")


def test_detached_one_credit_renders_as_singular():
    change = CreditDetached(role="cast", name="Zendaya")
    assert render_summary(CreditsDetached(credits=(change,))) == render_summary(change)


def test_unknown_role_rejected_in_detachment():
    with pytest.raises(ValueError, match="producer"):
        render_summary(CreditsDetached(credits=(CreditDetached(role="producer", name="M P"),)))


# ── Now-available summary tests (NEU-1375, D-28) ─────────────────────────


def test_flatrate_reads_as_streaming():
    assert render_summary(
        NowAvailable(offers=(AvailableOn(monetization_type="flatrate", providers=("Netflix",)),))
    ) == ("Now streaming on Netflix.")


def test_rent_names_every_provider_carrying_it():
    assert render_summary(
        NowAvailable(
            offers=(AvailableOn(monetization_type="rent", providers=("Apple TV", "Prime Video")),)
        )
    ) == ("Available to rent on Apple TV and Prime Video.")


def test_buy_has_a_clause_of_its_own():
    assert render_summary(
        NowAvailable(offers=(AvailableOn(monetization_type="buy", providers=("Apple TV",)),))
    ) == ("Available to buy on Apple TV.")


def test_several_types_first_seen_together_read_in_box_order():
    """One observation that first sees a film under rent *and* flatrate is one card, and the
    clauses read in the order the where-to-watch box lists them (D-29) rather than in whichever
    order the poll's payload happened to emit."""
    assert render_summary(
        NowAvailable(
            offers=(
                AvailableOn(monetization_type="buy", providers=("Apple TV",)),
                AvailableOn(monetization_type="flatrate", providers=("Netflix", "Hulu")),
            )
        )
    ) == ("Now streaming on Netflix and Hulu. Available to buy on Apple TV.")


def test_an_unknown_monetization_type_is_rejected():
    with pytest.raises(ValueError, match="ads"):
        render_summary(
            NowAvailable(offers=(AvailableOn(monetization_type="ads", providers=("Tubi",)),))
        )


# --- trailers (D-35) -----------------------------------------------------------


def test_a_trailer_card_says_a_new_trailer_is_out():
    assert render_summary(TrailerReleased()) == "A new trailer is out."


def test_the_trailer_body_does_not_name_the_film_or_the_video():
    """The card sits under the film's own title, and TMDB's video `name` is editor-entered
    free text — the key rides on the event instead, as `EventOut.video_key` (NEU-1386)."""
    body = render_summary(TrailerReleased())

    assert "trailer" in body.lower()
    assert body.count(".") == 1


# --- production companies (EF-5, NEU-1433) --------------------------------------


def test_a_studio_attaching_joins_the_production():
    assert (
        render_summary(CompaniesAttached(companies=(CompanyAttached(name="Legendary Pictures"),)))
        == "Legendary Pictures joins the production."
    )


def test_several_studios_attaching_share_one_clause():
    """D-7: a film gaining its studio and its financier in one edit is one beat, and the body
    names both rather than repeating itself on two cards."""
    assert (
        render_summary(
            CompaniesAttached(
                companies=(
                    CompanyAttached(name="Legendary Pictures"),
                    CompanyAttached(name="Warner Bros. Pictures"),
                )
            )
        )
        == "Legendary Pictures and Warner Bros. Pictures join the production."
    )


def test_a_studio_detaching_is_no_longer_attached():
    assert (
        render_summary(CompaniesDetached(companies=(CompanyDetached(name="Legendary Pictures"),)))
        == "Legendary Pictures is no longer attached."
    )


def test_several_studios_detaching_share_one_clause():
    assert (
        render_summary(
            CompaniesDetached(
                companies=(CompanyDetached(name="A Studio"), CompanyDetached(name="B Studio"))
            )
        )
        == "A Studio and B Studio are no longer attached."
    )


def test_one_company_renders_as_the_singular_change():
    change = CompanyAttached(name="Legendary Pictures")

    assert render_summary(CompaniesAttached(companies=(change,))) == render_summary(change)
    detached = CompanyDetached(name="Legendary Pictures")
    assert render_summary(CompaniesDetached(companies=(detached,))) == render_summary(detached)


def test_a_company_body_does_not_name_the_film():
    """Where this departs from the EF-5 spec line's illustrative phrasing ("Legendary Pictures
    joins *Dune: Part Three*"): every body in this module leaves the title out, because the
    card renders under the film's own title on every surface that shows it."""
    body = render_summary(CompaniesAttached(companies=(CompanyAttached(name="Legendary"),)))

    assert "Film" not in body
    assert body.count(".") == 1
