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

**Shadow mode**:
The middle state of `LINK_RETRIEVAL_MODE`, between `off` and `on`: the roster path still
decides, while retrieval runs beside it and records what it *would* have offered. Because
retrieval is pure and needs no model call, this measures recall against the incumbent on live
traffic at full scale for no added cost — it is where the cutover evidence comes from.
_Avoid_: dry run (it records, and the records are the point), dark launch, A/B test (nothing is
split — both paths see every story).

**Retrieval probe**:
One shadow-mode observation: for a story the **roster linked**, where retrieval put that pick —
retrieved or not, at what rank, at what score, out of how many candidates. One row per linked
story, because a story both paths rejected is agreement with nothing to adjudicate. Its recall
rate is *not* truth on its own: the roster makes false positives, so retrieval declining to
surface one is a win that a bare percentage scores as a loss.
_Avoid_: sample, trace, shadow result (too vague — health is a shadow result too).

**Retrieval health**:
The per-run retrieval rates — zero-candidate share, cap saturation, mean candidate-set size —
counted over *every* story retrieval ran over, not just the linked ones. It carries the
denominator a **retrieval probe** cannot, and it is what replaced the briefed cache-ratio
guardrail (ADR-0010).
_Avoid_: retrieval metrics (that's the offline F1 gate), recall (health says nothing about
whether the right film was found).

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
