"""The one-line detail the entrypoint writes to `ingest_run.detail`."""

from upmovies.ingest.sweep import EnumerateResult, RefreshResult, sweep_detail


def test_reports_both_phases_distinctly():
    """A run that enumerated fine but refreshed nothing must be visible as such on
    `/admin/runs` — the phase that silently produces nothing is the one that costs the
    whole catalog-sourced-event feature (§6.2)."""
    detail = sweep_detail(
        EnumerateResult(seed_people=7519, candidates_found=40, admitted=0, withheld=40),
        RefreshResult(),
    )

    assert "enumerate:" in detail
    assert "refresh:" in detail
    assert "7519 seeds" in detail
    assert "0/0 refreshed" in detail


def test_counts_every_failure_the_phases_recorded():
    detail = sweep_detail(
        EnumerateResult(person_failures=2, candidate_failures=3),
        RefreshResult(selected=10, refreshed=8, dormant_selected=4, failures=2),
    )

    assert "5 failed" in detail
    assert "8/10 refreshed" in detail
    assert "4 dormant" in detail
    assert "2 failed" in detail


def test_names_the_phase_that_aborted():
    detail = sweep_detail(
        EnumerateResult(aborted=True, abort_error="aborted after 10 consecutive failures"),
        RefreshResult(),
    )

    assert "enumerate aborted: aborted after 10 consecutive failures" in detail


def test_a_clean_pass_says_nothing_about_aborting():
    assert "aborted" not in sweep_detail(EnumerateResult(), RefreshResult())
