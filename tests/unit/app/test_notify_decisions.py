"""The decision pass's rules that need no database: who is queued and who is suppressed, and
the run's detail line.

The pass itself is covered in `tests/integration/app/test_notify_pass.py`.
"""

from uuid import uuid4

from upmovies.app.services.notify_service import NotifyResult, Recipient, notify_detail


def test_a_deliverable_recipient_queues_and_everyone_else_is_suppressed():
    assert Recipient(user_id=uuid4(), deliverable=True).status == "queued"
    assert Recipient(user_id=uuid4(), deliverable=False).status == "suppressed"


def test_the_detail_line_reports_suppression_beside_the_queued_digests():
    line = notify_detail(
        NotifyResult(users_considered=4, events_considered=9, digests_queued=5, suppressed=3)
    )
    assert line == "notify: 9 events, 4 users, 5 digests, 3 suppressed, 0 failed"


def test_an_aborted_pass_says_so_on_the_same_line():
    line = notify_detail(
        NotifyResult(failures=10, aborted=True, abort_error="aborted after 10 consecutive failures")
    )
    assert line.endswith("; notify aborted: aborted after 10 consecutive failures")


def test_a_cold_start_line_does_not_read_as_a_quiet_day():
    """`0 digests` is what a healthy quiet night looks like too, so the first run ever has to
    say what it actually did."""
    assert notify_detail(NotifyResult(cold_start=True)) == (
        "notify: cold start — watermark established, nothing queued"
    )
