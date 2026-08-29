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

**Heartbeat**:
A write that says *this run is still alive*, as distinct from *this run produced something*.
`ingest_run.last_progress_at` carries both meanings and the second one is the newer: the sweep's
enumerate phase spends 30–70 minutes issuing one credits request per seed person without
admitting anything, so a column advanced only by output stays `NULL` through exactly the
stretch a staleness rule most needs to read. Every sweep per-item loop therefore ticks a
time-throttled heartbeat regardless of outcome, and `mark_stale_runs_cancelled` expires a run on
`COALESCE(last_progress_at, started_at)`. The invariant is what makes one tight window serve a
4-minute feeds pass and a 6-hour sweep alike: a live run is never quiet for longer than the
heartbeat interval. Cadence is in **time**, not items — the window it feeds is a time window,
and a per-N-items throttle makes the guarantee depend on how fast that particular loop happens
to be.
_Avoid_: progress (that's the output meaning, and the reason the column was ambiguous),
keepalive, liveness probe, ping (that's the deadman).

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
kept apart on purpose. Its counterpart is the **soft breach**, which watches a different rate
and does not fail anything.
_Avoid_: alert (nothing is sent; the deadman notices a ping that never came), threshold
breach (ambiguous with **T**, retrieval's score threshold), breach unqualified (there are two
tiers, and only one of them stops the chain).

**Soft breach**:
A run whose cap-saturation rate exceeds the warn threshold, flagged on the health row and
named in the run's detail line — **retrieval health**'s soft tier. It does not fail the run:
saturation is drift, and the daily chain is fail-fast. Where a **hard breach** says retrieval
has collapsed, this says **K** no longer fits the catalog — it is the signal that schedules a
retune (ADR-0010, NEU-1088). Stored per run rather than recomputed, because the threshold is a
setting that moves as the catalog grows and the question is what the run was judged by at the
time.
_Avoid_: saturation breach (the flag is about the rate, not the cap), warning (too vague — the
hard tier warns too).

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

**Governing release date**:
Per subject `(iso_3166_1, release_type)`, the earliest displayable `catalog.film_release_date`
row — `min(release_date)` over the current rows for that subject. It is the single value both
the movie page and the release calendar list (collapsed to one line per subject), and the value
a release-date event tracks: a card fires only when a changed date becomes the governing date
and that new governing date was not already present in the previous set (NEU-1206). The calendar,
being upcoming-only, excludes a category whose governing date is past rather than falling
through to a later date. It is the per-subject analogue of the **primary release date**, which
is country- and type-agnostic; the two must not be conflated.
_Avoid_: primary release date (TMDB's scalar, country-agnostic), earliest release (too vague).

### Undated film discovery

**Sweep**:
The scheduled pass that discovers and maintains films TMDB's dated `/discover/movie` roster
cannot reach. It runs as its own `ingest_run.kind` on its own schedule slot, roughly two hours
ahead of the daily pipeline — deliberately *not* a stage in it, because the daily chain is
fail-fast and a ~45-minute TMDB pass would take feeds, link and synthesize down with it. It has
**four** phases and needs all of them: **enumerate** (walk seed people's credits for undated
candidates), **refresh** (re-fetch every active film discover did not touch), **events** (card
the field changes refreshing produced), and **credits** (card the attachments it produced).
Dropping the refresh phase is the project's quietest failure mode — undated films sit outside
the discover window, so without it they are never re-read, never upserted, and no
catalog-sourced event ever fires. Running the two carding phases last, in the same pass, is what
makes a change TMDB published today a card today; running the whole sweep ahead of the daily
chain is what puts that card in front of `link` rather than behind it. The two card from
different tables and fail independently, so they count separately on `/admin/runs`.
_Avoid_: crawl, scan, discovery run (that's the enumerate phase alone), Path B job.

**Missing** (of a film or a person):
TMDB answers 404 for its id — the entry has been deleted or merged upstream. Deliberately not a
*failure*: a failure is a reason to worry about TMDB and counts toward the sweep's
consecutive-failure abort, whereas missing is terminal and counts toward nothing, because no
retry brings a deleted record back. Conflating the two is what aborted the 2026-08-11 sweep —
eleven missing films, sorted to the head of a stalest-first queue, read as an outage. The two
are counted separately on `/admin/runs` for the same reason: a climbing *missing* count is
catalog hygiene, a climbing *failed* count is an incident.
_Avoid_: deleted, dead, gone, orphaned (that's an `ingest_run` left `running`).

**Tombstone**:
The `tmdb_missing_at` stamp that records a film or person as **missing**, and takes it off the
work the sweep does every pass. Not a deletion and not a retirement: a tombstoned film keeps its
page, its events and its linked stories — films are never deleted (spec §4.4) — and a tombstone
is only ever a statement about *TMDB's* record, never about the production. Never a one-way
door, on the same argument §4.5 makes about dormancy: a film's tombstone expires onto the
reduced `SWEEP_DORMANT_REFRESH_DAYS` cadence so a restored entry can be found again, and a
person's is cleared by the ordinary person upsert the next time any film names them.
_Avoid_: soft delete, archive, blacklist, dead flag.

**Seed person**:
Someone whose credits the sweep enumerates: anyone holding a **seed-grade** credit — director,
writer (`Writer`/`Screenplay`), or top-5 billed cast — on an **active, non-dormant** film. 7,519
of them at a 1,435-film catalog. Producers are deliberately not seed-grade: an EP credit travels
far and says little about whether a project is real. The set is *self-expanding* — admitting a
film contributes its own credits back as seeds — and **dormancy** is what bounds it, so a
project that goes nowhere stops paying for its own people.
_Avoid_: tracked person, watched person, followed talent.

**Tranche**:
One seed grade's admission flag — `SWEEP_ADMIT_DIRECTORS`, `SWEEP_ADMIT_WRITERS`,
`SWEEP_ADMIT_CAST` — opened one at a time so a precision drop names the grade that caused it
rather than arriving as one undifferentiated jump. They sit under the master `SWEEP_ENABLED`,
which is kept separate on purpose: the master is the rollback, the tranches are the ramp, and a
sweep that enumerates and reports while admitting nothing is the state where all four are off.
Admission is per **film**, not per credit — one open tranche among the grades that reached a
candidate is enough.
_Avoid_: phase (that's enumerate/refresh/events/credits), stage, wave, cohort.

**Corroboration threshold**:
How many **distinct seed people** must reach an undated film before it may be admitted
(`SWEEP_CORROBORATION_THRESHOLD`). Distinct *people*, not credits — someone who both wrote and
directed a film corroborates it once. It is the third clause of the admission bar, alongside
status and seed-grade role, and it is the dial between the two things one director attachment
can be: the earliest signal the product sells, and a speculative TMDB entry. Not to be confused
with the **corroboration window**, which is about release-date stories agreeing with TMDB's
change history — same word, unrelated mechanism.
_Avoid_: confidence threshold, minimum seeds, corroboration window (that's the other one).

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
attributes to **"via TMDB"** where a story-sourced event lists outlets. Confidence follows the
field: a release-date or status change is `confirmed`, because ADR-0002 already makes TMDB the
system of record for its own scalar fields, while a credit — which any editor can add — sits
below a trade-sourced beat. When a trade story later clusters onto it, the LLM summary
supersedes the deterministic one and real sources appear — the card upgrades in place. It exists
because story supply is fixed at 8 trade feeds while the catalog is about to multiply: without
it, most admitted films would be permanently blank pages.
_Avoid_: synthetic event, system event, auto event, TMDB event (that's the source, not the kind).

**Credit attachment event**:
The catalog-sourced card raised when a seed-grade credit crosses into a film's credit set —
`crew_attached` for a director or writer, the existing `casting` for top-5 billed cast, both at
`rumored`. Keyed on the **observation**, not the person: every attachment sharing one
`changed_at` cards once, so a whole top-billed cast arriving between two ingests is one card
naming all of them rather than five. `crew_attached` is a new type and has to be registered
wherever the vocabulary is enumerated (`ck_event_type`, the arc's `_EVENT_STAGE`, the cluster's
`_STALE_EVENT_TYPES`) or it silently ranks below everything; it is deliberately *not* hidden,
because a director attaching to a film no trade has written about is the beat the whole expansion
exists for. A re-attachment after a **credit detachment event** is carded rather than suppressed
— the suppression check is removal-aware, so a person whose latest card is a removal re-enters
the timeline on the next attachment.
_Avoid_: casting event (that is one of the two types, not the pair), crew change, credit diff
(that is the history row it reads).

**Credit detachment event**:
The catalog-sourced card raised when a seed-grade credit leaves a film's credit set — a single
new type `credit_removed` covering director, writer and cast alike, at `rumored`. It is the
correction half of the credit-attachment beat: a brief attachment that TMDB later retracts would
otherwise live uncorrected forever, and a future notification system needs a removal event to
tell a user the attachment they were told about is gone. It is gated on a **prior visible
attachment card** (`crew_attached` or `casting`, any provenance, `occurred_at` before the
detachment): a first-observation baseline credit that was never carded has no published beat to
correct, so its departure emits no card. **Forward-dwell gate (NEU-1205):** a removal also cards
only if the person does *not* re-attach within `SWEEP_CREDIT_DWELL_DAYS` (default 3, 0 disables)
*after* it, so a **credit oscillation** flap (removed then rapidly re-added) is suppressed rather
than carded as a real departure; the removal is held until it is ≥ N days old so the forward
re-attach window is fully observed. The forward gate reads raw `catalog.film_credit_change` (not
`news.event`), because the flap's re-attachment is itself suppressed by removal-aware suppression
and so is never carded; it is scoped to the same seed-grade role so a cast→director move is two
events, not a flap. Like attachments it is keyed on the observation — one `credit_removed` card
per `(film, changed_at)`, all roles in one body — and it sits beside the attachment card (which
stays visible) in the collapsed "via TMDB" section, so the later "no longer attached" card *is*
the correction. `credit_removed` is deliberately unmapped in `_EVENT_STAGE` (a removal is not
forward progress and should not headline a day) and excluded from the LLM and story-dedup
vocabularies: the model cannot emit it, and a trade "X exits" story does not yet attach to it.
Reverses the original ADR-0014 decision that detachments were "recorded as history but never
carded" — NEU-1201's collapsed "via TMDB" section (which did not exist when that decision was
made) made the clutter concern moot.
_Avoid_: crew detached, cast departed (those imply the old split that only existed because
`casting` pre-existed), credit removal (too generic — a release date disappearing is also a
removal, and is out of scope), retraction, cancellation (those are about the *film*, not a
person).

**Credit oscillation** (of a TMDB credit):
A seed-grade credit flapping on and off a film over a few days — added → removed → added →
removed — typically an anonymous TMDB edit being reverted. Each flap used to card 1:1 under
NEU-1200, producing a join+depart chain per cycle. It is dampened by the **forward-dwell gate**
on the credit detachment event: a removal followed by a re-attachment within
`SWEEP_CREDIT_DWELL_DAYS` is suppressed (a flap), while a removal with no re-attachment cards
(a real departure). The discriminator is *forward* re-attachment, not *backward* dwell: a flap
and a real brief departure both have short backward dwell, so a backward gate would suppress
mostly real departures and re-create the stale-attachment bug. A held flap that ends in `removed`
still cards a real final departure later; one that ends in `added` self-corrects (the re-attachment
is suppressed by removal-aware suppression during the hold). The transient ≤N-day hold window is
the one cost — the latest *carded* event is briefly "attached" while TMDB says "removed" — bounded,
self-correcting, and confined to the collapsed section.
_Avoid_: flicker, churn (too vague — a credit changing departments is churn but not a flap),
vandalism (that is the *cause*, not the observable pattern), bounce.

**Double-carding**:
The failure mode where the story path and the catalog path both raise an event for one TMDB
change. They are independent by design — a story-triggered release-date event still needs
corroboration, a catalog-triggered one fires from the change alone — but they read the *same*
`film_field_change` row, so each has to check for the other's card — one rule read from opposite
ends, which is why the two checks have to move together. For a date move both sides work off the
**corroboration window**: the catalog path skips a change already covered by a *story*-borne
release-date event inside it, and the story path attaches to the most recent *catalog* event
inside it. A catalog card is matched at exactly its own change's timestamp, so a date that moves
twice in a week still gets two cards. Production milestones are matched on type alone: a film
enters production once, so a story running a month behind TMDB's status flip still belongs on
the existing card. Credits are matched on neither: the question is always **who**, answered by
`Event.subject_key` on both sides — the catalog path suppresses a person a card already names,
and a story naming someone a credit event carded joins that card rather than being dropped as a
restatement of it. A story about a director attaching comes back classified `casting`, because
the LLM has no `crew_attached` in its vocabulary, so the story side searches both credit types.
_Avoid_: duplicate event (too generic — a dedup within one path is also that), double-posting.

**Day-grouped events** (of a film page):
The film detail response groups a film's events into per-day `DayGroup` entries, each with `news_events` and `tmdb_events` — split by the same `EXISTS(event_story)` predicate the grouped feed uses (`_has_story()`). A `catalog`-provenance event that later gains a linked story migrates to `news_events`; this is the same contract as `news_backed` on `FeedDayItem`. The TMDB section is collapsed by default on the film page the same way it is on the feed. The film page is an **event log**, not a publication log: day groups are keyed by `Event.occurred_at` (when the change happened), and within each day events order by `occurred_at ASC, created_at ASC, id ASC` (NEU-1204). This diverges from the grouped feed, which keys day groups on `created_at` because the feed is a publication log (ADR-0016); the same event can therefore appear under different day headings on the two surfaces, by design.
_Avoid_: in-the-news section (that's `news_events`), TMDB section (that's `tmdb_events`), two-timeline display, split events.

**News-backed** (of a film-day):
The property the daily feed sections on: at least one of a film's visible events on that day has
a linked story, i.e. an outlet reported some part of the day's activity. Derived from
`EXISTS(event_story)` and pointedly **not** from `Event.provenance` — provenance is where an
event was *born* and is never mutated when a story attaches, so reading it would leave a
TMDB-carded beat that Variety later covered filed under TMDB forever, which is the promotion
this exists to expose. Classified by **any**, because the grouped feed's row is one (film, day):
a film-day with one Variety story and four TMDB changes is one row in the news-backed section
counting five, never the same film listed twice under one date heading. So `event_count` and
`top_event_type` stay computed over *all* of the film-day's events — the section answers "is
there reporting behind this film's activity today", not "is every item here reported".
_Avoid_: story-sourced (that is one event's provenance, not a day's rollup), sourced, reported.

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

**In play**:
A film that has neither released nor been called off — `active_film_clause` without its
dormancy term (`in_play_clause`). It exists for exactly one caller: the sweep's refresh phase,
which spans **both** sides of dormancy and so cannot ask the composed question. Everywhere the
working set is being *spent* — the retrieval index, the per-film query list, the seed-person
query — the word is **active**, and dormancy is part of what it means.
_Avoid_: active (that's the composed predicate), live, current, open.

**Discover watermark**:
The start of the last **finished** `tmdb` run, the timestamp the refresh set is cut against: a
film whose `updated_at` predates it is one discover did not reach. Finished, not succeeded, and
the difference is load-bearing — the refresh writes, so its own upserts lift a film past the
watermark that selected it. Pinned to the last *success*, a `tmdb` stage that stays broken
freezes the watermark, one sweep pass carries the whole catalog over it, and every later pass
selects nothing at all.
_Avoid_: cutoff, high-water mark, last sync, refresh cursor.
