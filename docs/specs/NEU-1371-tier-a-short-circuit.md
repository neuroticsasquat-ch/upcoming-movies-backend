# NEU-1371 — Tier-A short-circuit: a trade story releases a quarantined credit and merges into the news event

**Project:** bl: Consumer Pivot · **Milestone:** M5 · **Story:** NEU-1333
**Project spec:** `docs/specs/bl-consumer-pivot-project-spec.md` (D-5, INV-4)
**Blocked by:** NEU-1368 (quarantine hold)

## Problem

INV-4: if a Tier-A trade story confirms a credit currently in quarantine, the credit must
publish now and **merge** into the news event — never surface two days later as a stale
duplicate. Tier-A = a **trade feed** story; every story today comes from the eight curated
feeds, so every story qualifies.

Two facts make this non-trivial:

1. **Timing.** The sweep (which cards catalog credits) runs in its own slot ~2 h *before* the
   daily chain; `cluster` (which cards story credits) runs inside the daily chain. So a story
   about a credit is carded after that day's sweep pass, and the quarantined change would be
   carded by the *next* sweep unless something tells it not to.
2. **Type scoping.** The sweep's suppression check (`_uncarded_credits`,
   `credit_events.py`) only looks at cards of the group's own event type. The LLM has no
   `crew_attached` in its vocabulary, so a story about a director attaching is carded as
   `casting`; a `crew_attached` group for the same person would not see it and would card a
   duplicate. (The story→catalog direction already searches both types —
   `_catalog_dedup_target` in `news/catalog_events.py` — this ticket closes the catalog→story
   direction.)

Decided (2026-09-15): **stamp the change row.** `catalog.film_credit_change` gains
`carded_by_event_id`; a stamped change is published-by-story and the sweep never cards it. The
link also records which event published which change, which is what makes "we had it first"
answerable later.

## What to build

### 1. Schema

- `catalog.film_credit_change.carded_by_event_id UUID NULL` → FK `news.event.id` (ON DELETE
  SET NULL), index. Model first, then migration.

### 2. Forward direction — story after change (cluster stamps)

In `link/cluster.py`, immediately after a story-provenance event of type `casting` or
`crew_attached` is created **or** a story attaches to an existing one, call
`stamp_story_confirmed_changes(session, film_id, event)`:

- Select `film_credit_change` rows for the film with `change='added'`, `carded_by_event_id IS
  NULL`, `changed_at ≥ now - SWEEP_STORY_CONFIRM_DAYS` (default **14**, covers the quarantine
  window plus slack), joined to `catalog.person`, whose `normalize_name(person.name)` is in
  `event.subject_key`, and whose credit is still present in `film_credit` (same person, same
  seed-grade role).
- Set `carded_by_event_id = event.id` on each. Matching is by normalized name, the same key both
  paths already use; after M4 lands, a resolved `story_person.person_id` on the story is
  preferred when present (leave a clearly named seam; do not implement M4 here).
- Runs inside the cluster item's transaction; commit per story as today.

### 3. Backward direction — change after story (sweep stamps)

The trades often scoop TMDB: the story card exists first, the credit appears in TMDB days
later. In `load_attachment_backlog` (or a step right after it, before the quarantine gate),
for each pending `added` row look for a **story-provenance** event on the film, type in
`(casting, crew_attached)`, whose `subject_key` contains the person's normalized name and
whose `occurred_at ≥ changed_at - SWEEP_STORY_CONFIRM_DAYS`. If found, stamp the row with that
event and drop it from the backlog. One query for the whole pass, not per row.

### 4. Loader and suppression

- `load_attachment_backlog` excludes rows with `carded_by_event_id IS NOT NULL`.
- `_uncarded_credits` is unchanged in scope (its own-type rule stays; it now never sees a
  story-published change because the loader dropped it). No cross-type widening.
- Detachments (`credit_removed`, NEU-1200/1205) are unaffected: a removal of a
  story-published credit still gates on the prior *visible* attachment card, which the story
  card is (any provenance), and NEU-1347's supersession marks that story card.

### 5. "We had it first"

Nothing new to display in this ticket, but the data now supports it: a change row with
`carded_by_event_id` pointing at a **catalog**-provenance card that a story later attached to
(via ADR-0014 promotion) is a TMDB scoop; a row pointing at a **story**-provenance card is a
trade scoop. Record this reading in the model docstring so the M4/M7 tickets can use it.

### 6. Backfill

None. Pre-existing duplicates (story card + catalog card for the same person) are
grandfathered, consistent with NEU-1205's forward-only stance.

## Acceptance criteria

- Credit added day 0 (quarantine 72 h), trade story carded day 1 → the change row is stamped
  with the story event; the day-3 sweep pass cards nothing for that person; exactly one card
  exists.
- Story carded day 0, credit appears in TMDB day 2 → the sweep stamps the row at load time and
  cards nothing; exactly one card exists.
- A director story (LLM type `casting`) stamps a `crew_attached`-role change for the same
  person; no `crew_attached` card is raised later.
- A story naming person A does not stamp a change for person B on the same film; a change
  older than `SWEEP_STORY_CONFIRM_DAYS` is not stamped by a later story.
- A stamped change whose credit is later removed still produces a `credit_removed` card (gate
  satisfied by the story card) and marks the story card superseded (NEU-1347).
- `_uncarded_credits` tests unchanged and green; `task format`, then `task test && task lint
  && task typecheck` pass.

## Out of scope

- Using resolved person ids instead of names (M4 seam only).
- Any change to `_catalog_dedup_target` (story→catalog direction already works).
- A visible "first reported by" line on cards (M4/M7 may add it; the data is ready).
- Alerting: whether a story-published credit alerts is M7's decision pass (D-32) and requires
  resolution (M4) for person follows.
