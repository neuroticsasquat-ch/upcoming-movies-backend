# Catalog changes create events on their own, attributed to TMDB

**Status:** accepted
**Amends:** ADR-0002 (deletes its premise; its rule survives)

## Context

ADR-0002 made TMDB the source of truth for release dates and demoted the story to "the trigger
and the colour": an event fires only when a story arrives *and* TMDB's change history corroborates
it. It justified requiring the story trigger on an explicit premise:

> **Every tracked film already has a release date.** … `film.release_date IS NULL` is effectively
> never true for a tracked film. Condition "no date yet" is a near-empty set; **"the date changed"
> is the whole game.**

The undated-film expansion deletes that premise. "No date yet" becomes the majority set.

Two further facts make the story-trigger requirement untenable for the expanded catalog:

- **Story supply is fixed.** Google News is paused (ADR-0001); the corpus is 8 curated trade
  feeds, a cost independent of catalog size. Admitting more films brings in no more stories.
- **Coverage is already thin.** Only 117 of 1,435 films (8%) have any linked story. Admitting
  several thousand undated films — which the trades mostly do not write about — would leave the
  overwhelming majority of the catalog as permanently empty pages.

So under the existing rule, the expansion's entire visible output would be thousands of blank
film pages. The events are what make an admitted film worth having admitted.

## Decision

**A catalog change may create an event with no story.** Three triggers:

| Trigger | Infrastructure |
|---|---|
| `release_date` null→date, or moved | free — `film_field_change` already records it |
| `status` transition | free — same trigger; `status` is not in the denylist |
| Director / writer / cast attached | new — `catalog.film_credit` is delete-and-rebuild with no history |

ADR-0002's corroboration rule is **unchanged for story-triggered events**. This decision adds a
second, independent path to event creation; it does not relax the first.

**First observation is a baseline, never an event.** A film's credits are recorded on admission
with no events emitted; only subsequent diffs produce attachment events. This is a hard rule of
the credit-history contract, not an emergent property. `film_field_change` gets the equivalent
protection by accident — it is a `BEFORE UPDATE` trigger, so inserts write no history — and
accidents do not survive a rewrite. Without the rule, admitting 3,000 films would emit tens of
thousands of false "attached to direct" events on day one.

**Presentation.** `EventOut.summary` is a required `str` and every read path joins `EventSummary`,
so an event without a summary row is invisible everywhere. A catalog-sourced event therefore
writes a real `EventSummary` row with a **deterministic** body, produced by the event-creating
stage rather than the summarizer. `model` is a sentinel (`"deterministic"`), never a real model
id, so cost tables are not polluted with rows that made no call. The card attributes to
**"via TMDB"** in place of outlets, and confidence is marked below a trade-sourced beat because
TMDB is community-edited.

> **Refinement — 2026-08-11 (NEU-1081).** "Below a trade-sourced beat" holds for **credits**,
> which any editor can add. It does not hold for the primary scalar `release_date` and `status`:
> ADR-0002 already makes TMDB the system of record for its own fields, so a change to one of
> them is not a claim awaiting corroboration — it *is* the corroboration. Events raised from
> `film_field_change` are therefore `confirmed`. The community-editing caveat below stands for
> the credit half.

When a trade story later clusters onto the event, the LLM summary **supersedes** the deterministic
one and real sources appear. The card upgrades in place; `EventSummary` is keyed on the event, so
this needs no special path.

> **Amendment — 2026-08-28 (NEU-1200).** The original decision recorded credit detachments as
> history but **never carded them** — "no longer attached" was judged mostly TMDB reverting its
> own vandalism. That left a brief attachment living forever uncorrected in the timeline, and
> left no event for a future notification system to fire on when an attachment a user was told
> about disappears. **The credit half is reversed: detachments are now carded** as a new
> catalog-sourced event type `credit_removed`, sitting beside the attachment card in the
> collapsed "via TMDB" section (NEU-1201, which did not exist when this decision was made, made
> the clutter concern moot). The attachment card stays visible — the later dated "no longer
> attached" card *is* the correction; no per-row hidden/superseded state is introduced. A removal
> is gated on a prior visible attachment card (`crew_attached`/`casting`, any provenance) so a
> first-observation baseline credit's departure — never published — emits no card. The attachment
> suppression check is made removal-aware so a re-attachment after a removal cards again. The
> release-date half is **not** reversed: a date disappearing is not recorded at all (no history
> row), so there is nothing to card — that stays a separate ticket. See the spec at
> `docs/specs/NEU-1200-credit-removal-events.md`.

> **Amendment — 2026-08-29 (NEU-1205).** NEU-1200's carding rule has no dwell filter, so a
> TMDB credit that oscillates (added → removed → added → removed over a few days) produces a
> four-card chain — the exact noise this ADR originally declined to card for. The fix is a
> **forward-dwell gate with an N-day hold**, *not* the backward-dwell gate the ticket first
> proposed. **Backward dwell** (card a removal only if the prior attachment is ≥ N days old) was
> rejected on the data: it cannot tell a flap (removed then re-attached) from a real brief
> departure (removed, never re-attached) — both have short dwell — so at any N it suppresses
> mostly *final* departures, re-creating the stale-attachment bug NEU-1200 was built to fix (at
> N=7: ~12 flaps dampened, ~27 stale tails created). **Forward dwell** cards a removal only if
> the person does *not* re-attach within N days *after* it: a flap is suppressed, a real
> departure always cards. It is surgical (gates the flaps, cards the finals, no stale tail) at
> the cost of an N-day publication hold — low-harm, since removal cards are collapsed-section
> `rumored` and the film page groups by `occurred_at` (NEU-1204) so the card still appears under
> the correct day. The forward gate reads raw `catalog.film_credit_change` (not `news.event`),
> because a flap's re-attachment is itself suppressed by the removal-aware suppression and so is
> never carded; it is scoped to the same seed-grade role so a cast→director move is two real
> events, not a flap. **N = 3** (`SWEEP_CREDIT_DWELL_DAYS`, 0 disables, reverts to plain
> NEU-1200): the max forward re-attach gap among dwell-eligible flaps in the data is 2.001 days,
> so N=3 catches 100%; N must be < `SWEEP_EVENT_LOOKBACK_DAYS` so a held removal stays in the
> rolling window when it becomes eligible (the backfill backstops a mis-tuned N). **Transient
> invariant violation, accepted:** during the ≤N-day hold the removal is not yet carded, so the
> latest *carded* event is still the attachment while TMDB says removed — bounded, self-correcting,
> confined to the collapsed section, and strictly better than the alternatives. Backfill is
> **forward-only**: already-carded removals (including the reported Maya Boyd 4-card chain) are
> grandfathered, not destructively cleaned. See the spec at
> `docs/specs/NEU-1205-dampen-credit-oscillation.md`.

## Considered alternatives

- **Keep requiring a story trigger.** Rejected: it is the status quo, and against an expanded
  catalog with fixed story supply it produces thousands of empty pages — the expansion would
  deliver nothing user-visible.
- **Hide catalog events until a story corroborates them** (`HIDDEN_EVENT_TYPES`). Rejected for
  the same reason with an extra step: the pages stay empty and the machinery is built for
  nothing.
- **Generate the summary with the LLM.** Rejected: there are no stories to summarize, so the
  model would be inventing prose from a field diff — cost and fabrication risk for no gain over
  a template.
- **Record `model` as the real summariser id.** Rejected: no call is made, and the pricing and
  `ingest.llm_call` tables are the system's cost ledger.
- **Emit events on first observation.** Rejected — see the baseline rule above.

## Consequences

- ADR-0002's premise is void; its **rule** survives intact, because a null→date transition was
  always recorded as a change and the unified rule already subsumed the null branch.
- Catalog-sourced events import TMDB's editorial noise into the product. Mitigated by lower
  confidence and explicit attribution, not eliminated — a vandalised credit can card, and (after
  NEU-1200) so can its removal. A flapping credit produces a card per flap, all in the collapsed
  "via TMDB" section at `rumored`; accepted as the cost of correction over silence.
- The two paths read the same `catalog.film_field_change` row, so each has to check for the
  other's card or one date move raises two events. The catalog reader skips a change already
  covered by a story-borne release-date event inside the corroboration window; the story path
  attaches to the catalog event raised from the change that corroborated it. The rules are one
  rule read from opposite ends and have to move together.
- The credit-change history is new infrastructure, and its baseline-vs-change contract is the
  single most safety-critical part of this project.
- The feed gains a class of card with no outlets. The frontend must handle an empty sources array
  as a designed state rather than a degenerate one.
