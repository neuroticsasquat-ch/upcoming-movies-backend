"""The one-line detail the entrypoint writes to `ingest_run.detail`."""

from upmovies.ingest.sweep import (
    CreditEventResult,
    EnumerateResult,
    FieldEventResult,
    RefreshResult,
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
    )

    assert "enumerate:" in detail
    assert "refresh:" in detail
    assert "7519 seeds" in detail
    assert "0/0 refreshed" in detail


def test_counts_every_failure_the_phases_recorded():
    detail = sweep_detail(
        EnumerateResult(person_failures=2, candidate_failures=3),
        RefreshResult(selected=10, refreshed=8, dormant_selected=4, failures=2),
        FieldEventResult(changes_read=6, events_created=2, skipped=4),
        CreditEventResult(),
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
    )

    assert "enumerate aborted: aborted after 10 consecutive failures" in detail


def test_a_clean_pass_says_nothing_about_aborting():
    assert "aborted" not in sweep_detail(
        EnumerateResult(), RefreshResult(), FieldEventResult(), CreditEventResult()
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
    )

    assert "events: 4 carded from 31 changes, 27 already carded, 1 failed" in detail


def test_names_the_field_change_phase_when_it_aborts():
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(aborted=True, abort_error="aborted after 10 consecutive failures"),
        CreditEventResult(),
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
    )

    assert "credits: 3 carded from 12 attachments, 9 already carded, 1 failed" in detail


def test_names_the_credit_phase_when_it_aborts():
    detail = sweep_detail(
        EnumerateResult(),
        RefreshResult(),
        FieldEventResult(),
        CreditEventResult(aborted=True, abort_error="aborted after 10 consecutive failures"),
    )

    assert "credits aborted: aborted after 10 consecutive failures" in detail
