# Upcoming Movies — Domain Context

The backend ingests upcoming-film metadata (TMDB) and entertainment news, links stories to
films, clusters them into events, and summarizes those events for the tracker.

## Language

### Pipeline shape

**Stage**:
One of the four named steps at which the pipeline consults a model: **link** (story → film),
**cluster** (linked stories → event), **summarize** (event → published summary), and
**source_judge** (unknown outlet domain → trust tier). Each has its own quality bar and is
configured independently. The set is closed, and enforced as such in the schema.
_Avoid_: step, phase, pass.

**Logical call**:
One request a stage makes of a model, counted **once** however many HTTP attempts it took.
Its latency is the total wall-clock the pipeline experienced, retries included — that is the
number that decides whether a provider is viable for a daily publish — and its retries are
kept visible separately as `attempts` rather than hidden inside that figure. The grain of
`ingest.llm_call`, and the unit a `parse_ok` outcome hangs off.
_Avoid_: request, attempt (that's the retry, not the call), round-trip.

**Truncated**:
A reply that stopped because it reached the `max_tokens` ceiling rather than because the model
finished. Recorded per logical call, and only meaningful *beside* `parse_ok`: unparseable output
means two opposite things depending on it — the reply ran out of room (raise the ceiling, or
shrink the batch), or the model cannot hold the output format (change model). Providers spell
the signal differently, `finish_reason: "length"` against `stop_reason: "max_tokens"`, so what is
stored is the predicate rather than either spelling. It is recorded and never acted on: the four
stages keep their four different parse-failure behaviours.
_Avoid_: cut off, incomplete, over-length, capped.

**Provider**:
The host a stage's model is served by — `anthropic`, `deepinfra`, `deepseek`. It is a
*separate* axis from the model: the premise of the gateway work is that two providers serve the
same open weights at different prices and under different cache economics, so a model id alone
identifies nothing billable. Hence `(provider, model)` is the key everything cost-shaped hangs
off — `pricing._RATES`, the `ingest.llm_call.provider` column, and the per-stage `*_PROVIDER`
settings. Rates are never shared across providers, and never defaulted from one.
_Avoid_: host, vendor, reseller, backend.

**Gateway**:
The one object a pipeline holds instead of a client: it answers "which **provider** serves this
**stage**, and what do I call it with". It exists because a pipeline is not a stage — one
`run_link_ingest` spans link, source_judge and cluster — so a single client threaded down could
never carry three independently configured providers. Its clients are pooled per provider, not
per stage, and it **never falls back**: a stage whose provider has no credential fails, because
answering it from another provider would attribute that provider's cost, latency and coverage
to the one that never ran. Failing is a *startup* event, not a run one — both
entrypoints (the app's lifespan and the scheduled-task process) assert every stage's
`(provider, model)` is priced and credentialed before any work begins, so a misconfiguration
costs a container that will not start rather than a publish half-committed when it stops. It
resolves the provider only; the model stays an argument, so the eval harnesses keep sweeping
models without going through config.
_Avoid_: router, proxy, factory, client (that's the thing it hands out).

**Stable prefix**:
The part of a stage's request that a builder promises does not vary between calls — in
practice its instructions. Every adapter must serialize it first and unmodified, which is the
one requirement that satisfies both explicit caching (Anthropic marks a `cache_control`
breakpoint) and automatic caching (DeepInfra and DeepSeek match the longest byte-identical
prefix). Named as a requirement, not a mechanism: `Prompt.stable_prefix` is what the four
builders emit, and no builder names `cache_control`. Whether a prefix is actually cached is a
fact about the provider's minimum cacheable length, not about the request — today all four
sit below it, and the contract being inert is expected.
_Avoid_: system block, cached block, `cache_control` (all vendor mechanism, adapter-internal).

**Total stage failure**:
A stage that produced **nothing at all** yet recorded failures — the case where per-item
isolation degenerates into "discard everything and report success". It is the one condition a
pipeline *chooses* to finalize a run `failed` on, reading its own counters, as opposed to a
crash the background wrapper catches. It is what makes the daily chain abort before the next
stage and ping the deadman `/fail`. A *partial* failure is not one: the survivors committed,
so the run succeeded.
_Avoid_: outage (that's the cause, not the observation), batch failure.

**Lossy stage**:
A stage whose failed item gets no second chance, so one failure is enough to declare a total
stage failure. **link** is the only one: a story it never links ages out of the recency window
and is never retried, so a missed run really can lose it.
_Avoid_: retryable (that's the opposite), transient.

**Self-healing stage**:
A stage whose failed item is re-selected unconditionally on the next run, so declaring a total
stage failure takes more than one failed candidate. **cluster** and **summarize** are both.
Without that denominator, one permanently bad film or event on an otherwise empty backlog
would fail the whole chain every day and publish nothing at all. **source_judge** is neither:
it is a link sub-stage that keeps no counters and is not guarded.
_Avoid_: idempotent (that's about re-running, not about failures).

### Candidate retrieval

**Candidate set**:
The small set of films offered to the **link** stage for one story, selected by lexical match
before the model sees anything. A story's candidate set is its own — two stories in the same
batch carry different ones, and the model names a film by its position within that story's set.
_Avoid_: shortlist, roster (that was the full-catalog prompt prefix this replaced), matches.

**Squash-fold**:
A title or headline lowercased and stripped of *all* punctuation and whitespace, so
`Naga Bandham` and `Nagabandham` both become `nagabandham`. Compared as a substring, not for
equality, and only for titles of at least six folded characters — short ones would match inside
unrelated words. It is the whole of retrieval's normalization story beyond tokenization.
_Avoid_: slug, normalize (too broad — tokenization normalizes too), fuzzy match.

**Initialism collapse**:
A run of two or more `<letter><separator>` pairs — `.` or `/`, letters only — read as one word
before tokenizing, so `F.A.S.T.` yields `fast` and `S/H/V` yields `shv`. Applied to titles and
story text alike, because the index can only join spellings both sides tokenize the same way.
Declines below three collapsed characters: the collapse invents a token the text never
literally contains, and at two `U.S.` would match every film with `us` in its title.
_Avoid_: acronym expansion (nothing is expanded), abbreviation handling (too broad).

**Out-of-list reply**:
A classifier reply naming a candidate number that story's set does not offer — a number
valid only in another story's list, or none at all. Rejected rather than coerced to the
nearest candidate, and stamped `link_note = out-of-list` so a numbering regression stays
distinguishable from the `no-match` the model never actually reached. Expressible only
because numbering is per story; the global roster had no local scope to fall outside of.
_Avoid_: invalid index, hallucinated film (it may name a real film — just not this story's).

**Labelable set**:
The films a validation-fixture row may name as its `about` subject: the tracked films active
on the fixture's pin date, and no others. It exists because it must equal the set the eval
harness can *score* against — label a film outside it and `validate_linking` aborts rather than
report a miss it would read as the link path's failure. Enforced by one shared
`indexed_tmdb_ids`, so the labeling scripts and the harness cannot drift apart.
_Avoid_: roster (that was the full-catalog prompt prefix, and it is deleted), tracked films
(the catalog holds released ones too), candidate set (that is per story, and narrower).

**Retrieval miss**:
The correct film absent from a story's candidate set. The failure mode retrieval introduces,
and an unforgiving one: **link** is lossy, so a missed story is not linked late, it is lost.
Distinct from the model declining a film it *was* shown, which is an ordinary rejection.
_Avoid_: false negative (that's the model's error, not retrieval's), recall failure.

**Zero-candidate rejection**:
A story rejected because no film cleared the threshold — decided by the lexical rule alone,
with no model call. Carries `link_note = no-candidates` so it stays distinguishable from a
`no-match` the model actually reached, and is deliberately **not** counted as
`StageCounts.processed`, since no classifier decided it — it feeds **retrieval health**
instead. (The run row's own `items_processed` does count it: the stage really did dispose of
the story. Only the total-failure guard reads `StageCounts`.)
_Avoid_: auto-reject, unmatched, filtered.

**Shadow mode** (historical):
The middle state of the retired `LINK_RETRIEVAL_MODE`, between `off` and `on`: the roster path
still decided, while retrieval ran beside it and recorded what it *would* have offered. Because
retrieval is pure and needs no model call, this measured recall against the incumbent on live
traffic at full scale for no added cost — it is where the cutover evidence came from. The mode,
the flag and the roster were all deleted at NEU-1004; the term survives because the evidence and
the **retrieval probe** rows it produced are still read.
_Avoid_: dry run (it recorded, and the records were the point), dark launch, A/B test (nothing
was split — both paths saw every story).

**Retrieval probe** (historical):
One shadow-mode observation: for a story the **roster linked**, where retrieval put that pick —
retrieved or not, at what rank, at what score, out of how many candidates. One row per linked
story, because a story both paths rejected is agreement with nothing to adjudicate. Its recall
rate is *not* truth on its own: the roster makes false positives, so retrieval declining to
surface one is a win that a bare percentage scores as a loss. Nothing has written one since the
cutover — there is no second opinion left to adjudicate against — but `/admin/runs` still reads
the rows for the runs that produced them.
_Avoid_: sample, trace, shadow result (too vague — health is a shadow result too).

**Retrieval health**:
The per-run retrieval rates — zero-candidate share, cap saturation, mean candidate-set size —
counted over *every* story retrieval ran over, not just the linked ones. It carries the
denominator a **retrieval probe** cannot, and it is what replaced the briefed cache-ratio
guardrail (ADR-0010).
_Avoid_: retrieval metrics (that's the offline F1 gate), recall (health says nothing about
whether the right film was found).

**Hard breach**:
A run whose zero-candidate rate exceeds the ceiling, finalizing it `failed` so the daily
chain aborts and the deadman is pinged — **retrieval health**'s hard tier, and the thing that
was built instead of the briefed cache-ratio alert (ADR-0010). Read on every `link` run —
retrieval is the only link path since NEU-1004 — and only above a minimum story count, because
a rate over a handful of stories cannot tell a collapse from a quiet news day. It
watches a *rate*, which is exactly what a **total stage failure** refuses to do — two guards,
kept apart on purpose.
_Avoid_: alert (nothing is sent; the deadman notices a ping that never came), threshold
breach (ambiguous with **T**, retrieval's score threshold).

### News sources

**Trade feed**:
A curated RSS/Atom source from an established entertainment outlet (e.g. Variety, Deadline),
hard-coded in `news/feeds.py`. The canonical, always-on spine of news ingestion.
_Avoid_: source (too vague), publisher (that's the resolved outlet, see below).

**Google source**:
A story ingested via a Google News search rather than a trade feed — either a broad topic
**query** (casting / release date / trailer / greenlight) or a **per-film search** keyed on a
tracked film's title. Paused on a trial basis behind `NEWS_GOOGLE_ENABLED`.
_Avoid_: aggregator feed.

**Outlet**:
The real publisher behind a story. For a trade feed it's the feed itself; for a Google source
it must be *resolved* by decoding the Google redirect URL to a publisher domain.
_Avoid_: publisher, site.

### Trust & gating

**Source-quality gate**:
The link-stage sub-stage that, per story, resolves the outlet domain, LLM-judges the trust
tier of any unknown domain, and hard-drops admin-blocked stories before they can form or join
an event.
_Avoid_: source filter.

**Effective tier**:
The trust tier the gate actually acts on for a domain: `blocked`, `trusted`, `acceptable`, or
`low`. Precedence is admin override, then the cached LLM verdict, then a neutral default.
_Avoid_: rating, score.

**Admin override**:
A manual per-domain trust decision (`none` / `block` / `allow` / `trust`) set from the admin
Sources page. Wins over the LLM verdict.
_Avoid_: manual rating.

### Story lifecycle

**Story**:
A single ingested news item (unique by URL). Carries a `link_status` of `pending`, `linked`,
or `rejected`.
_Avoid_: article, item, post.

**Event**:
A cluster of stories about the same real-world development for a film. What the tracker
summarizes and displays.
_Avoid_: cluster (that's the act of forming an event), group.

**Attach**:
Adding a newly linked story to an event that *already exists*, rather than forming a new one
— a beat already logged gaining another report of itself. The counterpart to forming an
event. A story attaches to exactly one event.
_Avoid_: merge (that's the defect below), append, link (that's the story→film step).

**Split beat**:
One real development recorded as two or more events, because stories that report it failed
to reach the same event. Shows up as duplicate entries in a film's timeline: untidy, but no
information is lost.
_Avoid_: duplicate event, fragmentation.

**Over-merge**:
Two genuinely distinct developments recorded as a single event. The more serious failure of
the pair — the swallowed beat disappears from the timeline entirely, so information is lost
rather than merely repeated.
_Avoid_: false merge, collision.

### Release-date events

**Release-date event**:
An event recording that a film's release date became known or moved. It is grounded in
**TMDB state**, not in a story's wording: it may exist only when TMDB's own release date has
actually changed (a first date being assigned counts as a change from "none"). A story is the
*trigger and the colour* for a release-date event — never the source of truth for the date.
_Avoid_: date announcement, release news.

**Corroboration**:
Confirmation, from TMDB's release-date change history, that a claimed release-date change is
real. A release-date story is *corroborated* when TMDB records a matching change to the film's
primary release date within the **corroboration window** (a small number of days). Only
corroborated stories may form a release-date event.
_Avoid_: verification, confirmation (too generic).

**Restatement**:
A story that merely repeats the film's already-known release date rather than reporting a
change. Restatements never form a release-date event; they are the classic false-positive this
model exists to suppress.
_Avoid_: mention, recap.

**Held story**:
A release-date story that claims a date TMDB has not yet caught up to. Rather than being
rejected, it is *held* — left unlinked-to-any-event and re-evaluated on later runs — until TMDB
corroborates the change or the corroboration window lapses, at which point it is dropped as
**uncorroborated**. Holding exists because the trades usually break a date move before
community-edited TMDB reflects it.
_Avoid_: pending (that's a `link_status`), queued, deferred.

**Primary release date**:
TMDB's single scalar `release_date` for a film — the only date this model gates on. Per-country
/ per-type dates (the `film_release_date` table) are explicitly out of scope: a regional-only
move that leaves the primary date untouched does not form a release-date event.
_Avoid_: regional date, theatrical date (those are the out-of-scope per-country values).

### Undated film discovery

**Sweep**:
The scheduled pass that discovers and maintains films TMDB's dated `/discover/movie` roster
cannot reach. It runs as its own `ingest_run.kind` on its own schedule slot, roughly two hours
ahead of the daily pipeline — deliberately *not* a stage in it, because the daily chain is
fail-fast and a ~45-minute TMDB pass would take feeds, link and synthesize down with it. It has
**two** phases and needs both: **enumerate** (walk seed people's credits for undated candidates)
and **refresh** (re-fetch every active film discover did not touch). Dropping the refresh phase
is the project's quietest failure mode — undated films sit outside the discover window, so
without it they are never re-read, never upserted, and no catalog-sourced event ever fires.
_Avoid_: crawl, scan, discovery run (that's the enumerate phase alone), Path B job.

**Seed person**:
Someone whose credits the sweep enumerates: anyone holding a **seed-grade** credit — director,
writer (`Writer`/`Screenplay`), or top-5 billed cast — on an **active, non-dormant** film. 7,519
of them at a 1,435-film catalog. Producers are deliberately not seed-grade: an EP credit travels
far and says little about whether a project is real. The set is *self-expanding* — admitting a
film contributes its own credits back as seeds — and **dormancy** is what bounds it, so a
project that goes nowhere stops paying for its own people.
_Avoid_: tracked person, watched person, followed talent.

**Seed grade**:
The role classes that both qualify a person as a seed *and* qualify a candidate film for
admission. It is checked twice, on purpose: once on the person (do we follow them at all) and
once on their role **on the candidate film** — without the second check a "Special Thanks"
credit would drag in someone's short film.
_Avoid_: role tier, credit weight, billing.

**Catalog-sourced event**:
An event created by a change in TMDB's own data — a release date assigned or moved, a status
transition, a credit attached — with **no story behind it**. It carries a deterministic
`EventSummary` (a template, never a model call: `model` is the sentinel `"deterministic"`) and
attributes to **"via TMDB"** where a story-sourced event lists outlets. Confidence sits below a
trade-sourced beat because TMDB is community-edited. When a trade story later clusters onto it,
the LLM summary supersedes the deterministic one and real sources appear — the card upgrades in
place. It exists because story supply is fixed at 8 trade feeds while the catalog is about to
multiply: without it, most admitted films would be permanently blank pages.
_Avoid_: synthetic event, system event, auto event, TMDB event (that's the source, not the kind).

**First observation**:
The first time the sweep reads a newly admitted film's credits. It is recorded as a **baseline
and emits no events** — a hard rule of the credit-history contract rather than something left to
fall out of the implementation. `catalog.film_field_change` gets the same protection by accident
(it is a `BEFORE UPDATE` trigger, so inserts write no history), and the credit history is being
built from scratch, where the accident does not repeat. Without the rule, admitting 3,000 films
would emit tens of thousands of false "attached to direct" events on day one.
_Avoid_: initial sync, backfill, seeding (that's the person set).

**Dormant**:
An undated film that has been quiet for N days — no `film_field_change` row *and* no linked
story. Dormant films leave `active_film_clause`, and with it the candidate retrieval index, the
per-film query list, and the seed-person query; they keep their page, their events, and a
**reduced-cadence refresh**. That last part is not a concession: detecting the change that
revives a film requires re-fetching it, so a dormancy that stopped the refresh would be a
one-way door. Keyed on **quiescence**, never on age — a film can be real and quiet for a year.
Dormancy is load-bearing for three separate cost curves at once, which is why N is measured
rather than picked.
_Avoid_: inactive (that's `active_film_clause`'s whole predicate), stale, archived, retired,
abandoned.

**Reachable**:
Whether the dated `/discover/movie` roster can see a film at all. Unreachable films — undated
ones, and dated ones sitting below the popularity floor where discover stops paging — are the
sweep's refresh set, identified by `film.updated_at` predating the last `tmdb` run. Scoping the
refresh on reachability rather than on datedness is what closes the **promotion gap**: a film
that finally gets a date but stays under the floor would otherwise fall out of the sweep without
ever falling into discover, and freeze permanently.
_Avoid_: in-window, discoverable, indexed.
