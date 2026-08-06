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
