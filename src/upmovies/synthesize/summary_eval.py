"""Offline summary-eval helpers (NEU-453). Pure, importable pieces used by
`scripts/validate_summaries.py`: load labeled cases, turn a case into the summarizer's
EventInput, assemble an LLM-judge request, and parse the judge's verdict. No DB, no network —
the live orchestration lives in the script."""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from upmovies.synthesize.summarizer import EventInput, StoryInput

_JUDGE_INSTRUCTIONS = """You grade a one-sentence news blurb for an upcoming-movies tracker on \
temporal and attribution correctness only. You are given today's date (`as_of_date`), the \
news beat TYPE, the source stories, the generated blurb, and pass/fail CRITERIA. Judge the \
blurb ONLY against the criteria and the obvious temporal facts (a beat reported now has \
already happened and cannot occur after `as_of_date`; a date belonging to a different beat, \
such as the film's release date, is not this beat's date). Ignore style, tone, and wording.

Return ONLY a JSON object, no prose or markdown:
{"pass": true | false, "reason": "<one short clause>"}"""


@dataclass(frozen=True)
class SummaryCase:
    name: str
    as_of_date: date
    event_type: str
    film_title: str
    stories: list[StoryInput]
    criteria: str


@dataclass(frozen=True)
class JudgeVerdict:
    passed: bool
    reason: str


def load_summary_cases(path: str) -> list[SummaryCase]:
    with open(path) as fh:
        data = json.load(fh)
    cases: list[SummaryCase] = []
    for item in data:
        stories = [
            StoryInput(title=s["title"], dek=s.get("dek", ""), source=s.get("source", ""))
            for s in item["stories"]
        ]
        cases.append(
            SummaryCase(
                name=item["name"],
                as_of_date=date.fromisoformat(item["as_of_date"]),
                event_type=item["event_type"],
                film_title=item["film_title"],
                stories=stories,
                criteria=item["criteria"],
            )
        )
    return cases


def event_for_case(case: SummaryCase) -> EventInput:
    return EventInput(
        event_type=case.event_type,
        film_title=case.film_title,
        source_updated_at=datetime.combine(case.as_of_date, datetime.min.time(), tzinfo=UTC),
        stories=case.stories,
    )


def build_judge_request(
    case: SummaryCase, blurb: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system = [{"type": "text", "text": _JUDGE_INSTRUCTIONS}]
    payload = {
        "as_of_date": case.as_of_date.isoformat(),
        "event_type": case.event_type,
        "film": case.film_title,
        "stories": [{"title": s.title, "dek": s.dek, "source": s.source} for s in case.stories],
        "blurb": blurb,
        "criteria": case.criteria,
    }
    messages = [{"role": "user", "content": json.dumps(payload)}]
    return system, messages


def parse_judge_verdict(raw: str) -> JudgeVerdict:
    """Read the judge's {"pass", "reason"} object. Anything unparseable is treated as a
    FAIL (a judge that did not answer cleanly must not silently pass a case)."""
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("pass"), bool):
            reason = obj.get("reason")
            return JudgeVerdict(passed=obj["pass"], reason=str(reason) if reason else "")
    return JudgeVerdict(passed=False, reason=f"unparseable judge output: {raw[:120]!r}")
