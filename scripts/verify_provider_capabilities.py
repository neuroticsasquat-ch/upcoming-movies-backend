"""Verify empirically what an OpenAI-compatible provider actually does — not what its docs say.

NEU-979 makes this a precondition rather than a nicety, because the gateway's whole premise is
that provider choice becomes measurable. Three things have to be true of a provider before any
number taken from it means anything (design §10):

1. **Cached tokens are reported, and populated.** The hard selection criterion. Cost has to be
   attributable per model per stage or a switch is unfalsifiable. Field names differ — DeepSeek
   has historically used `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens` where everyone
   following OpenAI uses `prompt_tokens_details.cached_tokens` — so this dumps the **raw** usage
   block and then checks that `openai_compat._usage_from` maps it onto disjoint `Usage` counts.
2. **`response_format` is honoured, not merely accepted.** Accepting the field and ignoring it
   is indistinguishable from honouring it unless you ask a question whose answer would
   *naturally* be prose. So this runs a control without the field and a treatment with it.
3. **JSON compliance on a real stage prompt**, measured with that stage's own predicate rather
   than a fresh one — compliance is output-format compliance, not quality (design §11).

No DB. Costs a few cents. Run in the container with the provider keys in `.env`:

    task shell
    python scripts/verify_provider_capabilities.py                 # both providers, flash
    python scripts/verify_provider_capabilities.py --provider deepseek --model deepseek-v4-pro

Findings are printed, not asserted: this is a measurement, and the unit suite must never touch
the network (`CLAUDE.md`). Re-run it when a provider changes its API or a new one is added.
"""

import argparse
import asyncio
import json
import pathlib
import sys
from dataclasses import replace

import httpx

from upmovies.config import get_settings
from upmovies.llm.gateway import credential_for
from upmovies.llm.openai_compat import _text_from, _to_wire, _usage_from
from upmovies.llm.pricing import price, rates_for
from upmovies.llm.registry import DEEPINFRA, DEEPSEEK, base_url_for
from upmovies.llm.types import Prompt, Usage
from upmovies.news.source_quality import build_judge_request, judge_output_parses

# The flash tier at each provider: the cheap model a high-volume stage would actually route to,
# and the one the eval matrix (NEU-985) starts from.
DEFAULT_MODELS = {
    DEEPSEEK: "deepseek-v4-flash",
    DEEPINFRA: "deepseek-ai/DeepSeek-V4-Flash",
}

# Comfortably above any published minimum cacheable prefix (Anthropic's floors are 2048-4096
# tokens; DeepSeek's cache works in 64-token blocks). Deliberately generous: the question here
# is whether caching reports *at all*, and a probe that lands under a floor answers nothing.
_PREFIX_TARGET_CHARS = 24_000

# A real domain sample for the JSON-compliance probe — the source_judge stage's actual input
# shape. Mixed tiers on purpose, so the model has genuine work to do rather than a rubber stamp.
_JUDGE_ITEMS = [
    {"domain": "variety.com", "headline": "Studio sets 2027 release for untitled sci-fi film"},
    {"domain": "deadline.com", "headline": "Director in talks to helm adaptation"},
    {"domain": "movie-news-daily.example", "headline": "10 MOVIES YOU MUST SEE (NUMBER 7!)"},
    {"domain": "hollywoodreporter.com", "headline": "Cinematographer joins production"},
    {"domain": "filmscoop.example", "headline": "Rumor: sequel casting shortlist leaks"},
]

_COMPLIANCE_RUNS = 10

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "llm"


def _stable_prefix() -> str:
    """A large, deterministic, byte-identical prefix.

    Deterministic matters more than it looks: prefix caching matches bytes, so anything varying
    per call — a timestamp, a shuffled list — would produce a cache miss that reads exactly like
    "this provider does not cache"."""
    line = (
        "Reference note: film production news is judged on outlet reputation, on whether the "
        "claim is sourced, and on whether it concerns an unreleased title. "
    )
    return (line * (_PREFIX_TARGET_CHARS // len(line) + 1))[:_PREFIX_TARGET_CHARS]


async def _post(client: httpx.AsyncClient, model: str, prompt: Prompt) -> httpx.Response:
    """Send exactly the body the shipped adapter would send, over a raw client.

    The request is built by `openai_compat._to_wire` rather than by a local copy: a probe whose
    wire shape is a *reimplementation* of the adapter's stops verifying the adapter the moment
    the two drift, and would do so silently. Only the transport is hand-rolled, and only because
    `complete_call` returns a mapped `CallResult` — this needs the raw body, which is the whole
    point of asking a provider what it reports rather than reading its documentation."""
    return await client.post("/chat/completions", json=_to_wire(model, prompt))


# --- 1. cached-token reporting -------------------------------------------------------------


async def probe_cache(
    client: httpx.AsyncClient, provider: str, model: str, *, capture: bool = False
) -> bool:
    """Two calls sharing a byte-identical prefix and differing in the payload only.

    Differing payloads are the point: a cache hit here is evidence of *prefix* caching, which is
    what the `Prompt` contract promises to engage. A hit on two identical whole requests would
    be consistent with plain response caching and would say nothing about the contract.

    With `capture`, the warm response is written verbatim to `tests/fixtures/llm/` so the unit
    suite's usage mapping is pinned to a body a provider really sent. The prefix is synthetic
    rather than a stage's, because no current stage prefix clears a cache floor — which is spec
    §3's anticipated consequence, now observed rather than predicted.

    Returns whether every mapping came out disjoint. This one probe gates the exit code, while
    the other two only print: a mapping that double-counts cached tokens misprices every row
    the provider will ever produce, which is a different kind of finding from a compliance rate
    a human has to weigh."""
    print(f"\n{'=' * 78}\n1. CACHED-TOKEN REPORTING — {provider} / {model}\n{'=' * 78}")
    prefix = _stable_prefix()
    print(f"stable prefix: {len(prefix)} chars, byte-identical across both calls")
    disjoint = True

    for label, payload in (("cold", "Summarize the note in one line."), ("warm", "Reply OK.")):
        prompt = Prompt(stable_prefix=prefix, user=payload, max_tokens=64)
        response = await _post(client, model, prompt)
        if response.status_code != 200:
            print(f"  [{label}] HTTP {response.status_code}: {response.text[:400]}")
            return False
        body = response.json()
        if capture and label == "warm":
            path = _FIXTURES / f"{provider}_chat_completion.json"
            path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
            print(f"  [{label}] captured verbatim -> {path}")
        usage = body.get("usage") or {}
        print(f"\n  [{label}] raw usage block as returned by the provider:")
        print("    " + json.dumps(usage, indent=2, sort_keys=True).replace("\n", "\n    "))
        mapped = _usage_from(usage)
        print(f"  [{label}] _usage_from -> {mapped}")
        total = (
            mapped.input_tokens
            + mapped.cache_read_input_tokens
            + mapped.cache_creation_input_tokens
        )
        reported = int(usage.get("prompt_tokens") or 0)
        disjoint = disjoint and total == reported
        print(
            f"  [{label}] disjointness check: input+cache_read+cache_write={total} "
            f"vs reported prompt_tokens={reported} "
            f"{'OK' if total == reported else '*** MISMATCH — would misprice ***'}"
        )
    return disjoint


# --- 2. response_format ---------------------------------------------------------------------


async def probe_response_format(client: httpx.AsyncClient, provider: str, model: str) -> None:
    """Control vs treatment on a question whose natural answer is prose.

    Asking a model that was already told "return JSON" whether `response_format` works cannot
    distinguish honoured from ignored. So the control's prompt says nothing about JSON, and
    establishes what the model does when left alone.

    Three cases, because "accepted" turns out not to be binary. DeepSeek rejects the field
    outright unless the word "json" appears somewhere in the prompt, so a treatment that avoids
    the word cannot tell rejection apart from a broken request — and one that uses it cannot
    tell `response_format` apart from the instruction. Running both separates them."""
    print(f"\n{'=' * 78}\n2. response_format SUPPORT — {provider} / {model}\n{'=' * 78}")
    question = "Name three films released in 2025 and say one sentence about each."
    # "json" appears only as a bare noun, with no instruction to *emit* it — enough to satisfy a
    # provider that grep's the prompt, not enough to be the thing producing the JSON.
    hinted = question + " (Context: this request is part of a json-based pipeline.)"
    cases = (
        ("control — no response_format, no 'json' in prompt", question, False),
        ("treatment A — response_format, no 'json' in prompt", question, True),
        ("treatment B — response_format, 'json' present as a noun", hinted, True),
        ("control B — 'json' present as a noun, no response_format", hinted, False),
    )

    for label, user, wants_json in cases:
        prompt = Prompt(
            stable_prefix="You are a film reference assistant.",
            user=user,
            max_tokens=400,
            json_object=wants_json,
        )
        response = await _post(client, model, prompt)
        if response.status_code != 200:
            print(f"  [{label}]\n    HTTP {response.status_code}: {response.text[:300]}")
            if wants_json:
                print("    -> REJECTED outright, not accepted-and-ignored")
            continue
        text = _text_from(response.json())
        try:
            json.loads(text)
            verdict = "parses as JSON"
        except json.JSONDecodeError:
            verdict = "prose / not JSON"
        print(f"  [{label}]\n    {verdict}: {text[:140]!r}")


# --- 3. JSON compliance on a real stage prompt ----------------------------------------------


async def probe_json_compliance(client: httpx.AsyncClient, provider: str, model: str) -> None:
    """The real `source_judge` prompt, graded by that stage's own `judge_output_parses`.

    Run twice — as production builds it, and again with `response_format` — because the
    difference between the two is the only thing that says whether setting it is worth doing."""
    print(
        f"\n{'=' * 78}\n3. JSON COMPLIANCE ({_COMPLIANCE_RUNS} runs each) — {provider} / {model}"
        f"\n{'=' * 78}"
    )
    base = build_judge_request(_JUDGE_ITEMS)
    totals = Usage()

    for label, wants_json in (("as production builds it", False), ("with response_format", True)):
        prompt = replace(base, json_object=wants_json)
        ok = 0
        errors: list[str] = []
        failures: list[str] = []
        for _ in range(_COMPLIANCE_RUNS):
            response = await _post(client, model, prompt)
            if response.status_code != 200:
                errors.append(f"HTTP {response.status_code}: {response.text[:120]}")
                continue
            payload = response.json()
            totals += _usage_from(payload.get("usage") or {})
            text = _text_from(payload)
            if judge_output_parses(text):
                ok += 1
            else:
                failures.append(text)
        print(f"  [{label}] parse_ok {ok}/{_COMPLIANCE_RUNS}")
        for err in errors[:3]:
            print(f"    error: {err}")
        # A rate alone doesn't say *how* the output was wrong, and the two failure modes want
        # opposite responses: a truncated reply is a `max_tokens` problem, while a well-formed
        # reply of the wrong shape is a prompt/`response_format` mismatch.
        for sample in failures[:2]:
            print(f"    non-compliant sample ({len(sample)} chars): {sample[:400]!r}")

    try:
        cost = price(totals, rates_for(provider, model), batch=False)
        print(f"\n  tokens across this probe: {totals}\n  priced at the table's rates: ${cost:.6f}")
    except KeyError:
        print(
            f"\n  tokens across this probe: {totals}\n  no _RATES entry for ({provider}, {model})"
        )


async def verify(provider: str, model: str, *, capture: bool = False) -> bool:
    """Run all three probes against one provider. False if it was skipped or mismapped.

    The credential comes from the same resolver the gateway uses, so a provider this script
    reports on is one a stage could actually be pointed at. It differs only in what absence
    means: a script skips a provider it has no key for, where `Gateway` refuses to run the
    stage (`llm/gateway.py`)."""
    key = credential_for(get_settings(), provider)
    if not key:
        print(f"\n!! {provider}: no {provider.upper()}_API_KEY in the environment — skipped")
        return False
    async with httpx.AsyncClient(
        base_url=base_url_for(provider),
        headers={"Authorization": f"Bearer {key}"},
        timeout=120.0,
    ) as client:
        disjoint = await probe_cache(client, provider, model, capture=capture)
        await probe_response_format(client, provider, model)
        await probe_json_compliance(client, provider, model)
    return disjoint


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=sorted(DEFAULT_MODELS), action="append")
    parser.add_argument("--model", help="override the model id (only with a single --provider)")
    parser.add_argument(
        "--capture-fixtures",
        action="store_true",
        help="overwrite tests/fixtures/llm/<provider>_chat_completion.json with the warm "
        "cache-hit response, verbatim",
    )
    args = parser.parse_args()

    providers = args.provider or sorted(DEFAULT_MODELS)
    if args.model and len(providers) != 1:
        parser.error("--model needs exactly one --provider")

    ok = [
        await verify(p, args.model or DEFAULT_MODELS[p], capture=args.capture_fixtures)
        for p in providers
    ]
    # Non-zero if any requested provider was skipped or reported token counts our mapping
    # cannot make disjoint. The other two probes stay advisory — a compliance rate is a
    # judgement, a mispriced token count is a bug.
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
