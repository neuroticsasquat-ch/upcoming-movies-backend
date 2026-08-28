# NEU-1200 — Credit removal events: card TMDB credit detachments

## Problem

When a seed-grade credit (director, writer, top-5 billed cast) attaches to a film, the sweep's
credits phase cards a catalog-sourced event — `crew_attached` for director/writer, `casting` for
cast, both at `rumored` confidence (ADR-0014). TMDB is community-edited, so credits are often
removed later: the person left the project, or was never actually attached. ADR-0014's original
decision recorded those detachments as history (`catalog.film_credit_change`, `change="removed"`)
but **never carded them** — "no longer attached" was judged mostly TMDB reverting its own
vandalism. The result: a brief attachment lives forever in the film's timeline, uncorrected.

Two things have changed since that decision:

1. **NEU-1201 (merged)** split the film page and feed into "In the news" (expanded) and "via
   TMDB" (**collapsed by default**) sections. A removal event is `provenance="catalog"` with no
   story → it lands in the collapsed `tmdb_events` section automatically. The "clutter" concern
   that motivated the original "don't card" decision is now mitigated by a collapse that didn't
   exist when the decision was made.
2. **Future notifications** (no scaffolding exists yet; ADR-0016 pre-decides `created_at` as the
   trigger) need a removal event to notify a user when an attachment they were told about
   disappears. Without a removal card, there is nothing to fire on.

The fix: card detachments as a new catalog-sourced event type `credit_removed`, sitting beside
the attachment card in the collapsed "via TMDB" section. The later dated "no longer attached"
card **is** the correction — the attachment card stays visible, and the removal card corrects
it. No per-row hidden/superseded state, no frontend change.

**Reverses ADR-0014** for the credit half. The release-date half is untouched: a date
disappearing is not recorded at all (no history row), so there is nothing to card — that stays
a separate ticket if ever wanted.

## Scope

- **Backend only.** The frontend renders events generically: `EventOut.event_type` is plain
  `string`, the sole label map (`labels.ts:22-43`) has a title-case fallback (`"credit_removed"`
  → badge **"Credit Removed"**), both card renderers print `summary` verbatim, `provenance` →
  "via TMDB" is generic, `confidence` is not rendered, and grouping is by `news_backed`/`has_story`
  (not `event_type`). A `credit_removed` event routes into `tmdb_events` with zero frontend change.
  One optional cosmetic line (`credit_removed: "Credit removed"` in `labels.ts`) is not required.
- **Credits only.** Release-date withdrawals are out of scope (no withdrawal rows exist).
- **Event now, notifications deferred.** The removal event is notification-ready (a future
  system keyed on `created_at` per ADR-0016 picks it up); no notification/subscription machinery
  is built in this ticket.
- **Story-side dedup deferred.** Removal cards are catalog-only for now; a trade "X exits"
  story does not attach to the removal card. This is a known limitation, deferred to a future
  ticket.

## Acceptance criteria

- A seed-grade credit detachment cards a `credit_removed` event **when the person has a prior
  visible attachment card** (`crew_attached` or `casting`, any provenance) with
  `occurred_at < detachment.changed_at`. Detachments of first-observation baseline credits
  (which were never carded) emit no removal card.
- A story-sourced attachment card counts toward the gate: a trade casting report that carded P
  before TMDB had the credit, then TMDB removes P, produces a `credit_removed` card to correct
  the published story report.
- One `credit_removed` card per observation `(film_id, changed_at)` — all roles (director, writer,
  cast) removed in one TMDB observation share a single card, mirroring how attachments group.
- The removal event has: `event_type="credit_removed"`, `provenance="catalog"`,
  `confidence="rumored"`, `occurred_at = detachment.changed_at`, `region=None`,
  `subject_key = [normalize_name(name) for each gated person]`.
- The removal card carries a deterministic summary (sentinel `model="deterministic"`) with
  role-specific wording; "via TMDB" attribution is carried by `provenance`, not in the body.
- A re-attachment after a removal **is carded** (not suppressed): the attachment suppression
  check is removal-aware. The invariant is *the latest card for a person reflects their current
  attachment state.* A re-attachment without an intervening removal is still suppressed (unchanged).
- The `uq_event_catalog_change` unique constraint `(film_id, event_type, occurred_at) WHERE
  provenance='catalog'` prevents double-carding on re-reads, same as attachments.
- A one-time backfill cards pre-existing uncorrected detachments (the backlog) at ship time.
  Steady-state relies on the shared `SWEEP_EVENT_LOOKBACK_DAYS` rolling window (default 7), which
  catches every fresh removal within ~1 day of the sweep observing it.
- `task test && task lint && task typecheck` pass.

### Non-goals / out of scope

- **Release-date withdrawals.** A date disappearing is not recorded in `film_field_change` at all
  (no history row). Handling it requires new withdrawal-recording infrastructure; separate ticket.
- **Notifications / subscriptions.** No notification, subscription, watchlist, or following
  machinery exists. This ticket builds the removal event (notification-ready) and defers all
  notification delivery.
- **Story-side dedup for removals.** A trade "X exits the project" story does not attach to the
  removal card. The LLM has no departure type in its vocabulary (a departure story classifies as
  `casting`/`other`). Deferred as a known limitation.
- **Hiding, strikethrough, or superseding the attachment card.** The attachment card stays
  visible; the removal card sits beside it as the correction. No new `Event` column for
  hidden/superseded/retracted state.
- **Frontend changes.** The frontend renders `credit_removed` generically (badge, summary,
  "via TMDB" attribution, `tmdb_events` routing all work with zero changes).
- **`credit_removed` in the LLM vocabulary.** The LLM can never emit `credit_removed` (mirrors
  `crew_attached`, which is also LLM-invisible). Removal cards are catalog-only.

## Technical decisions

### 1. New event type `credit_removed`

A single new `event_type` covering all roles (director, writer, cast). Attachments split into
`crew_attached`/`casting` only because `casting` pre-existed; removals have no pre-existing type,
so one is cleaner than two.

**Vocabulary registration — 4 places must stay in sync** (the same discipline `crew_attached`
followed):

| Place | File | Treatment |
|-------|------|-----------|
| DB check constraint `ck_event_type` | `news/models.py:80-84` + migration | Add `'credit_removed'` to the `IN` list |
| Arc stage map `_EVENT_STAGE` | `public/arc.py:23-34` | **Deliberately unmapped.** A removal is not forward progress and should not headline a film-day. Unmapped → ranks `-1` in `most_significant_event_type`, below every other type (same as `first_look`/`other`). |
| Cluster stale-stage set `_STALE_EVENT_TYPES` | `link/cluster.py:52-58` | **Not added.** A removal is not a stale-stage beat; it must not be dropped as stale when a film has wrapped. |
| LLM valid-type set `_VALID_TYPES` | `link/cluster.py:42-51` | **Not added.** The LLM cannot emit it. |
| Catalog event-type sets `CREDIT_EVENT_TYPES` / `CATALOG_EVENT_TYPES` | `news/catalog_events.py:42,47` | **Not added.** Story-side dedup is deferred; keeping the removal type out of these sets means `_catalog_dedup_target` does not search it. |
| New constant | `news/catalog_events.py` | `CREDIT_REMOVED_EVENT_TYPE = "credit_removed"` — the shared vocab home, referenced by the carding phase. |

**Migration** mirrors `b7c31d9a4e02_add_crew_attached_event_type.py`: DROP `ck_event_type`, ADD
with `'credit_removed'` in the `IN` list. Downgrade: `DELETE FROM news.event WHERE event_type =
'credit_removed'` first (rows can't satisfy the old constraint), then DROP/ADD the old constraint.

### 2. Gate: card a removal only when the person has a prior visible attachment card

A removal is carded only if the detaching person has a prior attachment card — `crew_attached`
or `casting`, **any provenance** (story cards count) — with `occurred_at < detachment.changed_at`.
This excludes detachments of first-observation baseline credits (which were never carded, so
there is no published beat to correct), and matches the notification framing (only notify a
removal of something that was published).

The gate is **per-person**, checked individually within a group: three cast departing where one
was never carded still cards the other two.

**Known edge case:** a trade story reports P's casting (story card, `occurred_at` = story date)
after TMDB already removed P (detachment `changed_at` earlier). The story card's `occurred_at` >
detachment `changed_at`, so the gate fails and no removal card is emitted. The story card stays
uncorrected. This is rare (trade reports an attachment TMDB already retracted) and no worse than
today; accepted for the first version.

### 3. Removal-aware attachment suppression

The existing attachment suppression (`_uncarded_credits` in `credit_events.py:186-207`) suppresses
a re-attachment when the person is already named on an attachment card of the same type. Because
we are **not** hiding the attachment card, a person who attaches → detaches → re-attaches would
have their re-attachment suppressed by the still-visible original attachment card — leaving the
timeline ending at "no longer attached" while the person is currently attached again.

**Fix:** replace the set-membership check (`recorded_subject_names`) with a **temporal** check.
For each person in an attachment group, find their most recent event among
`(event_type, 'credit_removed')` — i.e. their own attachment type plus the removal type — by
`occurred_at DESC` (with `created_at DESC` as tiebreaker). Suppress only if the most recent is
the attachment type. If the most recent is `credit_removed` (they were removed) or there is no
prior card, do not suppress — the re-attachment is news.

This preserves the existing scoping: an actor-director is still carded separately for cast and
directing (the check for a `casting` group searches `('casting', 'credit_removed')`, not
`('crew_attached', 'credit_removed')`), so cross-type suppression is unchanged. It only un-blocks
re-attachment after a removal of the **same** role.

**Invariant:** *the latest card for a person reflects their current attachment state.*

### 4. Deterministic summary templates

New dataclasses in `synthesize/deterministic.py`, mirroring `CreditAttached`/`CreditsAttached`:

- `CreditDetached(role: str, name: str)` — no `character` field (the credit history doesn't
  record it for removals).
- `CreditsDetached(credits: tuple[CreditDetached, ...])`.
- Add both to the `CatalogChange` union.

New renderer `_render_detached_role(role, people)`, following the `_render_role` pattern
(`deterministic.py:172-189`):

| Role | One person | Multiple |
|------|-----------|----------|
| director | `{name} is no longer attached to direct.` | `{names} are no longer attached to direct.` |
| writer | `{name} is no longer attached to write.` | `{names} are no longer attached to write.` |
| cast | `{name} departs the cast.` | `{names} depart the cast.` |

New `_render_detachments(change)` groups by role, strongest first (`ROLE_ORDER`: director,
writer, cast), joining clauses with spaces — identical structure to `_render_credits`.

New `render_summary` match arms for `CreditDetached` (wraps in `CreditsDetached`) and
`CreditsDetached`.

**`TEMPLATE_VERSION` bump:** `"deterministic-2"` → `"deterministic-3"` (`deterministic.py:39`),
so a body can be traced back to the phrasing that produced it.

"via TMDB" attribution is carried by `Event.provenance="catalog"` (the frontend's `SourceLinks`
renders it when `sources` is empty); no "via TMDB" string in the body, same as all other
deterministic templates.

### 5. Event metadata

Mirrors attachments and ADR-0016:

| Field | Value | Rationale |
|-------|-------|-----------|
| `event_type` | `"credit_removed"` | New dedicated type |
| `provenance` | `"catalog"` | Born from a TMDB change, no story |
| `confidence` | `"rumored"` | An anonymous editor deleting a credit is even less authoritative than adding one; `confirmed` is reserved for fields where TMDB is system of record (ADR-0002) |
| `occurred_at` | `detachment.changed_at` | When TMDB changed, not when the sweep carded it — same as attachments |
| `created_at` | `now()` (server default) | When the sweep published the card — the notification axis per ADR-0016 |
| `region` | `None` | Credits are not region-scoped |
| `subject_key` | `[normalize_name(name) for each gated person]` | The "who", same normalization both paths use |

### 6. Pipeline wiring

In `pipeline_run.py`, immediately after `run_credit_attachment_events` (line 234), call
`run_credit_detachment_events` with the same `session_factory`, `run_id`, `now`,
`lookback_days`, and `failure_threshold`. It runs in the credits phase, sharing the same
`ingest_run` row — same contract as the other phases (one session per item, `record_progress`,
abort after N consecutive failures, no `finalize_run`).

A new `CreditDetachmentResult` dataclass mirrors `CreditEventResult` but with
`detachments_read` instead of `attachments_read`. Add it to `_finalize_sweep`'s signature and
the aborts list. Add a clause to `sweep_detail` (`ingest/sweep/summary.py`):
`f"credit removals: {detached.events_created} carded from {detached.detachments_read}
detachments, {detached.skipped} already carded, {detached.failures} failed"`.

### 7. One-time backfill

A script `scripts/backfill_credit_removals.py` cards the pre-ship backlog: all `film_credit_change`
rows with `change='removed'` (no lookback window), each gated on a prior attachment card. It
calls the same carding logic as the steady-state phase with `since` unset (all history).
Idempotent via `_already_carded` + `uq_event_catalog_change`. Run once at ship via
`task shell` → `python scripts/backfill_credit_removals.py`.

The ship-day feed spike (all backfill removals land on one `created_at` day) is expected, not a
defect, per ADR-0016: "a backfill *is* a publication event."

### 8. ADR-0014 amendment + CONTEXT.md update

The reversal of "detachments are never carded" is hard to reverse, surprising without context,
and a real trade-off — record it as a dated amendment to ADR-0014 (NEU-1200) and update
CONTEXT.md: amend the "Credit attachment event" entry (remove the "never carded" sentence) and
add a "Credit detachment event" term.

## Implementation plan

### Migration

1. `task makemigration -- "add credit_removed event type"` — DROP/ADD `ck_event_type` with
   `'credit_removed'` added; downgrade `DELETE`s `credit_removed` rows first (template:
   `b7c31d9a4e02`).
2. Add `'credit_removed'` to the `CheckConstraint` string in `news/models.py:80-84`.
3. `task migrate`.

### Event-type registration

1. `news/catalog_events.py`: add `CREDIT_REMOVED_EVENT_TYPE = "credit_removed"` constant.
2. Do **not** add it to `_EVENT_STAGE`, `_STALE_EVENT_TYPES`, `_VALID_TYPES`, `CREDIT_EVENT_TYPES`,
   or `CATALOG_EVENT_TYPES` (all deliberate — see Technical decision 1).

### Detachment carding phase (`ingest/sweep/credit_events.py`)

1. Add `DetachedCredit` and `DetachmentGroup` dataclasses (mirror `AttachedCredit`/`CreditGroup`,
   but the group's `event_type` is always `CREDIT_REMOVED_EVENT_TYPE`).
2. Add `load_detachment_backlog(session, *, since)` — reads
   `FilmCreditChange.change == CREDIT_REMOVED` in the window, joined to `Person`, filtered to
   seed-grade via `credit_role`. (Mirror `load_attachment_backlog:128-168`, swapping the
   `change` filter and updating the docstring.)
3. Add `group_detachments(detachments)` — one group per `(film_id, changed_at)`. Single type, so
   all roles in one group. (Mirror `group_attachments:103-125`.)
4. Add `_has_prior_attachment_card(session, *, film_id, person_name, before) -> bool` — EXISTS an
   `Event` with `event_type.in_(("crew_attached", "casting"))`, `subject_key` contains
   `normalize_name(person_name)`, and `occurred_at < before`. Any provenance.
5. Add `_card_detachment_group(session, *, group) -> bool` — for each person, check the gate;
   keep only gated persons; if none remain, return False; check `_already_carded` (idempotent);
   create the `Event` + `write_deterministic_summary` with `CreditsDetached`. (Mirror
   `_card_group:210-246`.)
6. Add `run_credit_detachment_events(...)` — mirrors `run_credit_attachment_events:249-313`
   with `CreditDetachmentResult` (`detachments_read` field).

### Removal-aware suppression (`ingest/sweep/credit_events.py`)

1. Add `_latest_credit_event_type(session, *, film_id, person_name, event_type) -> str | None`
   — the `event_type` of the most recent `Event` (by `occurred_at DESC, created_at DESC`) among
   `(event_type, CREDIT_REMOVED_EVENT_TYPE)` whose `subject_key` contains
   `normalize_name(person_name)`, or `None`.
2. Replace the body of `_uncarded_credits` (lines 186-207): instead of
   `recorded_subject_names` set-membership, call `_latest_credit_event_type` per person. Suppress
   (exclude) a person iff their latest credit event type is `event_type` (an attachment). If it's
   `credit_removed` or `None`, keep them (the re-attachment is news).
3. Update the docstring to state the invariant and the removal-awareness.

### Deterministic summary (`synthesize/deterministic.py`)

1. Add `CreditDetached` / `CreditsDetached` dataclasses.
2. Add to `CatalogChange` union.
3. Add `_render_detached_role` and `_render_detachments`.
4. Add `render_summary` match arms.
5. Bump `TEMPLATE_VERSION` to `"deterministic-3"`.

### Pipeline wiring (`pipeline_run.py` + `ingest/sweep/summary.py`)

1. Import `run_credit_detachment_events` and `CreditDetachmentResult`.
2. Call `run_credit_detachment_events` after `run_credit_attachment_events` (line 234), same args.
3. Thread `detached` through `_finalize_sweep` (signature + aborts list).
4. Add the `credit removals:` clause to `sweep_detail`.

### Backfill script (`scripts/backfill_credit_removals.py`)

1. `asyncio.run` + `SessionLocal`; read all `removed` rows, group, gate, card. Or call
   `run_credit_detachment_events` with a very wide `lookback_days` (e.g. 3650).
2. Idempotent; safe to re-run.

### CONTEXT.md + ADR-0014

1. Amend CONTEXT.md "Credit attachment event" entry; add "Credit detachment event" term.
2. Amend ADR-0014 with a dated NEU-1200 reversal note.

## Tests

### Detachment carding (`tests/integration/ingest/sweep/test_credit_events.py`)

- `test_detachment_cards_when_prior_catalog_attachment` — film with a prior `crew_attached` card
  for P, then a `removed` row → `credit_removed` card created naming P.
- `test_detachment_cards_when_prior_story_attachment` — story-sourced `casting` card for P, then
  a `removed` row → `credit_removed` card created (story cards count toward the gate).
- `test_detachment_skipped_when_no_prior_attachment` — baseline credit (never carded) removed →
  no `credit_removed` card.
- `test_detachment_gate_requires_attachment_before_detachment` — attachment card with
  `occurred_at` after the detachment's `changed_at` → no removal card.
- `test_detachment_one_card_per_observation` — director + cast removed in one observation (same
  `changed_at`) → one `credit_removed` card naming both, summary has both clauses in role order.
- `test_detachment_already_carded` — re-read a window with an already-carded removal → skipped.
- `test_detachment_older_than_lookback` — removal older than the window → never read.
- `test_detachment_event_metadata` — verify `event_type`, `provenance`, `confidence`,
  `occurred_at`, `region`, `subject_key` on the created event.
- `test_detachment_summary_via_tmdb_attribution` — verify the deterministic body per role and
  that `EventSummary.model == "deterministic"`.

### Removal-aware suppression (`tests/integration/ingest/sweep/test_credit_events.py`)

- `test_reattachment_after_removal_is_carded` — P attaches (carded), detaches (removal card),
  re-attaches → re-attachment card created (not suppressed).
- `test_reattachment_without_removal_still_suppressed` — P attaches (carded), attaches again →
  second suppressed (unchanged from today).
- `test_reattachment_after_removal_different_role_unaffected` — P attaches as director, detaches,
  attaches as cast → cast attachment card created (the director-line removal does not suppress a
  cast attachment; cross-type scoping unchanged).
- `test_actor_director_still_carded_separately` — P attaches as cast (carded), attaches as
  director → director card created (cross-type suppression unchanged).

### Deterministic summary (`tests/unit/synthesize/test_deterministic.py` or equivalent)

- `test_render_detached_director_singular` / `plural`
- `test_render_detached_writer_singular` / `plural`
- `test_render_detached_cast_singular` / `plural`
- `test_render_detached_multi_role_group` — director + cast removed in one observation → both
  clauses in `ROLE_ORDER` sequence.
- `test_template_version_bumped` — `TEMPLATE_VERSION == "deterministic-3"`.

### Pipeline (`tests/integration/test_pipeline_run.py`)

- `test_sweep_runs_detachment_phase` — verify `run_credit_detachment_events` is called with the
  shared `lookback_days`.
- `test_sweep_detail_includes_detachment_clause` — the detail line includes the
  `credit removals:` clause.
