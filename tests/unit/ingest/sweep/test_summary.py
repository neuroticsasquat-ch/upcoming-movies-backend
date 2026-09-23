"""The one-line detail the entrypoint writes to `ingest_run.detail`."""

from collections import Counter

from upmovies.ingest.sweep import (
    CollectionEventResult,
    CompanyEventResult,
    ConfirmEventResult,
    CreditDetachmentResult,
    CreditEventResult,
    EnumerateResult,
    FieldEventResult,
    RefreshResult,
    ReleaseEventResult,
    sweep_detail,
)


def test_reports_every_phase_distinctly():
    """A run that enumerated fine but refreshed nothing must be visible as such on
    `/admin/runs` — the phase that silently produces nothing is the one that costs the
    whole catalog-sourced-event feature (§6.2)."""
    detail = sweep_detail(
        EnumerateResult(seed_people=7519, candidates_found=40, admitted=0, withheld=40),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "enumerate:" in detail
    assert "refresh:" in detail
    assert "7519 seeds" in detail
    assert "0/0 refreshed" in detail


def test_the_collections_clause_reports_carded_held_and_read():
    """EF-5's franchise half (NEU-1434). No burst count beside it, unlike the companies
    clause: the phase runs no burst check."""
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(changes_read=9, events_created=2, skipped=1, held=6),
        ConfirmEventResult(),
    )

    assert "collections: 2 carded from 9 changes, 1 already carded, 6 held, 0 failed" in detail
    assert "burst" not in detail.split("collections:")[1]


def test_a_collections_abort_is_named():
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(aborted=True, abort_error="aborted after 10 consecutive failures"),
        ConfirmEventResult(),
    )

    assert "collections aborted: aborted after 10 consecutive failures" in detail


def test_counts_every_failure_the_phases_recorded():
    detail = sweep_detail(
        EnumerateResult(person_failures=2, candidate_failures=3),
        RefreshResult(selected=10, refreshed=8, dormant_selected=4, failures=2),
        FieldEventResult(changes_read=6, events_created=2, skipped=4),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "5 failed" in detail
    assert "8/10 refreshed" in detail
    assert "4 dormant" in detail
    assert "2 failed" in detail


def test_names_the_phase_that_aborted():
    detail = sweep_detail(
        EnumerateResult(aborted=True, abort_error="aborted after 10 consecutive failures"),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "enumerate aborted: aborted after 10 consecutive failures" in detail


def test_a_clean_pass_says_nothing_about_aborting():
    assert "aborted" not in sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )


def test_reports_what_the_field_change_phase_carded():
    """The phase that turns TMDB's own changes into cards. A sweep that refreshed thousands
    of films and carded nothing is the shape of a broken reader, and it is only legible
    next to the refresh counts."""
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(refreshed=1200, selected=1200),
        FieldEventResult(changes_read=31, events_created=4, skipped=27, failures=1),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "events: 4 carded from 31 changes, 27 already carded, 1 failed" in detail


def test_names_the_field_change_phase_when_it_aborts():
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(aborted=True, abort_error="aborted after 10 consecutive failures"),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "events aborted: aborted after 10 consecutive failures" in detail


def test_reports_what_the_credit_phase_carded():
    """The two event phases read different tables and fail independently, so a reader that
    stopped carding credits has to be visible next to a healthy field-change count."""
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(refreshed=1200, selected=1200),
        FieldEventResult(changes_read=31, events_created=4),
        CreditEventResult(attachments_read=12, events_created=3, skipped=9, failures=1),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "credits: 3 carded from 12 attachments, 9 already carded, 0 held, 1 failed" in detail


def test_names_the_credit_phase_when_it_aborts():
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(aborted=True, abort_error="aborted after 10 consecutive failures"),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "credits aborted: aborted after 10 consecutive failures" in detail


def test_reports_admissions_against_skips_by_reason():
    """The ramp's operating question is "what did the open tranche let in, and what stopped
    the rest" — so the enumerate clause reads like the tmdb stage's, one reason at a time
    (NEU-1086). A run reporting only a total would leave "the tranche is closed" and "TMDB
    says they are all released" indistinguishable."""
    detail = sweep_detail(
        EnumerateResult(
            seed_people=1446,
            candidates_found=310,
            admitted=12,
            withheld=40,
            skipped_already_known=200,
            skipped_below_corroboration=50,
            skipped_dated_on_details=6,
            skipped_excluded_status=2,
        ),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "12 admitted" in detail
    assert (
        "skipped 298 (already_known=200, corroboration=50, dated=6, no_tranche=40, status=2)"
        in detail
    )


def test_a_pass_that_skipped_nothing_still_reports_a_skip_total():
    detail = sweep_detail(
        EnumerateResult(admitted=3),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "3 admitted, skipped 0," in detail


def test_reports_what_the_release_date_phase_carded():
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(changes_read=9, events_created=4, skipped=5),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "release dates: 4 carded from 9 changes, 5 already carded, 0 failed" in detail


def test_names_the_release_date_phase_when_it_aborts():
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(aborted=True, abort_error="aborted after 10 consecutive failures"),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "release dates aborted: aborted after 10 consecutive failures" in detail


def test_reports_the_attachment_histogram():
    """The distribution is only built during the sweep and only logged, and `docker exec`
    output never reaches `docker logs` — so the one artifact the M4 tuning ticket reads the
    threshold off (§4.3) has to land in `detail` or it is gone with the container."""
    detail = sweep_detail(
        EnumerateResult(
            admitted=12, attachment_histogram=Counter({1: 8421, 2: 932, 3: 100, 5: 40})
        ),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "seed attachments: 1×8421, 2×932, 3+×140" in detail


def test_a_pass_that_reached_no_candidates_says_nothing_about_attachments():
    """Dropped rather than rendered as an empty group, matching how `skip_counts` drops
    zero-valued reasons: the line is read at a glance."""
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    # The label, not the bare word: the credits clause legitimately says "from 0 attachments".
    assert "seed attachments:" not in detail


def test_the_credits_clause_reports_what_quarantine_is_holding():
    """NEU-1368. A held row and a window that read nothing both card zero events, so the
    count is the only thing on the line that tells the two apart."""
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(attachments_read=40, events_created=2, skipped=1, held=37),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "credits: 2 carded from 40 attachments, 1 already carded, 37 held, 0 failed" in detail


def test_reports_the_sanity_holds_apart_from_the_quarantine_count():
    """`held` and `holds` are not the same kind of number (D-8): the first merges quarantine's
    two reasons and is not a health signal, while every hold counted here is a reviewable claim
    against a named person."""
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(
            attachments_read=30,
            held=4,
            holds_new=25,
            holds_cleared=1,
            holds_expired=2,
        ),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "4 held" in detail
    assert "holds: 25 new, 1 cleared, 2 expired" in detail


def test_reports_a_quiet_holds_pass_as_zeroes_rather_than_dropping_the_clause():
    """The steady state is all three at zero, and a clause that vanished when nothing happened
    would make "no holds" indistinguishable from "this sweep predates holds"."""
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "holds: 0 new, 0 cleared, 0 expired" in detail


def test_reports_which_roles_reached_the_candidates():
    """The clause that makes a `no_tranche` count actionable — and, before
    `SWEEP_ADMIT_FOLLOWED` is flipped, the only sign that the followed enumeration reaches
    anything at all (D-50). Rendered in `ROLE_ORDER`, `followed` last."""
    detail = sweep_detail(
        EnumerateResult(
            candidates_found=9,
            withheld=9,
            role_histogram=Counter({"cast": 4, "director": 2, "followed": 3}),
        ),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "roles: director×2, cast×4, followed×3" in detail


def test_the_role_clause_is_dropped_when_nothing_was_reached():
    """Matching how `skip_counts` drops zero-valued reasons: a sweep that reached no
    candidates has nothing to say about how."""
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(),
        CreditDetachmentResult(),
        ReleaseEventResult(),
        CompanyEventResult(),
        CollectionEventResult(),
        ConfirmEventResult(),
    )

    assert "roles:" not in detail
