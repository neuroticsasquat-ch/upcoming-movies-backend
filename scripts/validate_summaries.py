"""Measure summary-prompt temporal/attribution correctness (NEU-453) against the labeled
fixture. Generates each blurb with the real summary_model, then grades it with an LLM-judge
(cluster_model, a stronger model). No DB. Run in the container with a real key in .env:

    task shell
    python scripts/validate_summaries.py [tests/fixtures/synthesize/summary_cases.json]

Prints per-case PASS/FAIL + the judge's reason and an aggregate. Costs one summary call and
one judge call per case."""

import asyncio
import sys

from upmovies.config import get_settings
from upmovies.llm.client import AnthropicClient
from upmovies.synthesize.summarizer import build_summary_request, parse_summary
from upmovies.synthesize.summary_eval import (
    build_judge_request,
    event_for_case,
    load_summary_cases,
    parse_judge_verdict,
)

DEFAULT_FIXTURE = "tests/fixtures/synthesize/summary_cases.json"
_MAX_TOKENS = 256


async def main(path: str) -> None:
    settings = get_settings()
    cases = load_summary_cases(path)
    passed = 0

    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        for case in cases:
            system, messages = build_summary_request(event_for_case(case), case.as_of_date)
            raw = await client.complete(
                model=settings.summary_model,
                system=system,
                messages=messages,
                max_tokens=_MAX_TOKENS,
            )
            try:
                blurb = parse_summary(raw)
            except Exception as exc:  # noqa: BLE001 - report, do not crash the whole run
                print(f"FAIL  {case.name}: summary unparseable ({exc})")
                continue

            j_system, j_messages = build_judge_request(case, blurb)
            j_raw = await client.complete(
                model=settings.cluster_model,
                system=j_system,
                messages=j_messages,
                max_tokens=_MAX_TOKENS,
            )
            verdict = parse_judge_verdict(j_raw)
            passed += verdict.passed
            mark = "PASS" if verdict.passed else "FAIL"
            print(f"{mark}  {case.name}")
            print(f"      blurb:  {blurb}")
            print(f"      reason: {verdict.reason}")

    print(f"\n=== SUMMARY EVAL ===  {passed}/{len(cases)} passed")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FIXTURE))
