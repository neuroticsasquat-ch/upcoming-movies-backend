import json
from datetime import date

from upmovies.synthesize.summary_eval import (
    JudgeVerdict,
    SummaryCase,
    build_judge_request,
    event_for_case,
    load_summary_cases,
    parse_judge_verdict,
)

FIXTURE = "tests/fixtures/synthesize/summary_cases.json"


def test_load_summary_cases_reads_fixture():
    cases = load_summary_cases(FIXTURE)
    assert [c.name for c in cases][:1] == ["trailer-not-release-date"]
    first = cases[0]
    assert isinstance(first, SummaryCase)
    assert first.as_of_date == date(2026, 7, 1)
    assert first.event_type == "trailer"
    assert first.stories[0].source == "News Mobile"
    assert "July 10" in first.criteria


def test_event_for_case_builds_event_input():
    case = load_summary_cases(FIXTURE)[0]
    event = event_for_case(case)
    assert event.event_type == "trailer"
    assert event.film_title == "Rural Action Drama"
    assert event.stories[0].title.startswith("Trailer")


def test_build_judge_request_includes_blurb_and_criteria():
    case = load_summary_cases(FIXTURE)[0]
    system, messages = build_judge_request(case, "A trailer was released on July 10.")
    assert isinstance(system, list) and system[0]["type"] == "text"
    payload = json.loads(messages[0]["content"])
    assert payload["blurb"] == "A trailer was released on July 10."
    assert payload["as_of_date"] == "2026-07-01"
    assert payload["criteria"] == case.criteria


def test_parse_judge_verdict_reads_pass_and_reason():
    v = parse_judge_verdict('{"pass": false, "reason": "states a future date as past"}')
    assert v == JudgeVerdict(passed=False, reason="states a future date as past")


def test_parse_judge_verdict_tolerates_prose_wrapping():
    v = parse_judge_verdict('Here: {"pass": true, "reason": "ok"} done')
    assert v.passed is True


def test_parse_judge_verdict_defaults_to_fail_on_garbage():
    v = parse_judge_verdict("not json at all")
    assert v.passed is False
    assert v.reason
