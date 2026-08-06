# Remove the Anthropic Message Batches path

**Status:** accepted — implementation tracked in NEU-971

## Context

`llm/client.py` carries two call surfaces: a sequential `complete_with_usage` and a batched
`complete_batch` that submits an Anthropic Message Batch, polls it to terminal, and returns
per-request results. Three stages have a batched twin (`_link_stage_batched`,
`_cluster_stage_batched`, `_summary_stage_batched`), each gated by a `*_USE_BATCHES` flag.

The batch path was built for its nominal **50% discount** and ADR-0003 explicitly kept it on
for that saving. Three facts have since undermined the premise.

**The discount was more than eaten by cache thrash.** Message Batches are best-effort within
24h, so a batch routinely outlives the 5-minute ephemeral cache TTL that the linking stage's
prompt prefix depends on. Measured cache read/write ratios ran **0.04–3.04 in batch mode
against 12–18x in standard mode**. On 2026-07-15 that wasted roughly **$10.02 of a $13.57 day
— about 74% of spend** — on cache writes that expired before anything read them. A 50%
discount does not survive paying 1.25x base rate for prefix writes that are never read.

**The latency is untenable for a daily publish.** The Batch API's SLA is up to 24 hours per
submission, and `link` → `cluster` → `synthesize` are dependent stages. Worst-case latency
compounds across submissions. ADR-0003 solved the *symptom* — a CI poll window that expired
before the batch did — by removing the external poller, but the underlying unbounded
wall-clock remained.

**Production has not used it since.** `docker-compose.prod.yml` defaults all three flags to
`false`. The Python settings defaults say `True` and the pipeline function defaults say
`False`. Three sources of truth, two of them wrong, none of them exercised.

A fourth fact made this urgent rather than merely tidy: the Message Batches API is
**proprietary in shape**. Every candidate provider in *backlotter: LLM Provider Gateway*
speaks OpenAI-compatible `/chat/completions`, and none has an equivalent. Carrying the batch
path into a provider-agnostic gateway means maintaining a permanently Anthropic-only branch
of the call surface — roughly half of `llm/client.py` — that no other provider can satisfy.

## Decision

Delete the batch path entirely: `complete_batch`, `BatchRequest`, `BatchResult`, the
`BatchCompleter`/`LinkClient` Protocols, the three `build_*_batch_request` builders, the three
`_*_stage_batched` pipeline stages and their dispatch, and the three `*_USE_BATCHES` settings.

Two things are deliberately **kept**:

- **`ingest.run_llm_usage.batched`** — historical rows recorded under batch mode are
  meaningful and must stay queryable. The column stays; the write becomes a constant `False`.
- **`pricing.py`'s `_BATCH_DISCOUNT` and `price(..., batch=)`** — needed to price those
  historical rows correctly.

## Considered alternatives

- **Keep the batch path, Anthropic-only, outside the neutral Protocol.** Rejected: it
  reintroduces exactly the vendor asymmetry the gateway exists to remove, and keeps three
  unexercised code paths alive with no production traffic to catch their rot.
- **Abstract batching across providers.** Rejected on measurement, not feasibility — batch
  mode *loses* money here, so building a portable version of it is paying to generalize a
  mistake.
- **Leave it in place and simply never enable it.** Rejected: it is ~100 of `client.py`'s 208
  lines plus three pipeline stages and their tests, all of which must be read, ported, and
  kept type-clean through the gateway work for no return.

## Consequences

- ADR-0003's batch-mode reasoning is superseded. See the amendment note on that ADR.
- The gateway has **one** call surface to make provider-neutral rather than two, which is the
  single largest scope reduction available to *backlotter: LLM Provider Gateway*.
- This is a one-way door. If the undated-film expansion later produces a genuinely
  batch-shaped workload — a large historical backfill, not the daily publish — it would be
  rebuilt from git history. That is accepted: the daily publish is the workload that exists,
  and it is latency-bound.
- Cost impact is nil, because production already runs sequential.
