# Ground release-date events in TMDB change history, not the story's claim

**Status:** accepted

## Context

Release-date events were created whenever the clustering LLM classified a beat as
`release_date`. A single deterministic guard (`link/cluster.py`) dropped only *exact*
restatements — a story whose LLM-extracted `claimed_date` matched `film.release_date` and had
been held longer than `LINK_RELEASE_RESTATE_DAYS` (default 7). Everything else fired an event on
the model's say-so.

In practice this over-produced. The site frequently carded "release date announcements" for
films that already had a date, where the "new" date was the *already-known* one only a few weeks
or months out — i.e. the story was a calendar/roundup restatement, not a change. The exact-match
guard missed these whenever the LLM extracted a `null` or slightly-off date, or the date was set
within the 7-day window. Restricting ingestion to trade feeds (ADR 0001) reduces the volume of
such stories but does not make the rule correct.

Two facts about the system shaped the decision:

- **Every tracked film already has a release date.** TMDB discover enumerates films *by*
  `primary_release_date` window, so `film.release_date IS NULL` is effectively never true for a
  tracked film. Condition "no date yet" is a near-empty set; **"the date changed" is the whole
  game.**
- **The trades lead TMDB.** Date moves are studio announcements broken by Deadline/Variety;
  community-edited TMDB reflects them hours-to-days later. So for the one event class worth
  keeping, the story normally arrives *before* TMDB corroborates it.

## Decision

A `release_date` event may be created **only when TMDB's change history (`film_field_change`)
records a change to the film's primary `release_date` within a corroboration window `W`**
(`LINK_RELEASE_CHANGE_WINDOW_DAYS`, default 14). A first date being assigned (`null → date`) is
recorded as a change, so this single rule subsumes both "no date yet" and "date changed" — there
is no separate null branch. The story is the trigger and the colour; TMDB is the source of truth
for the date.

Because the trades lead TMDB, a story asserting a change TMDB has not yet reflected is **held,
not rejected**: it is left linked-but-unclustered so the existing cluster loader re-evaluates it
on later runs, forming the event once TMDB catches up. A held story is dropped as
`release-date-uncorroborated` only after it ages past `W`.

To keep the hold queue precise, the LLM's `claimed_date` is used **only as a triage signal, never
to create an event**: a vetoed story is held only if it *claims a date different from
`film.release_date`*; plain restatements (`claimed_date == film.release_date`) and vague stories
(`claimed_date` null) are rejected immediately.

Scope is the **primary scalar `release_date` only**. Per-country / per-type dates
(`film_release_date`) are out of scope — that table is delete-then-reinsert on every ingest with
no change history, so region-aware detection needs new infrastructure and is deferred to its own
ticket.

The old exact-match restatement guard is fully replaced; `LINK_RELEASE_RESTATE_DAYS` is renamed
to `LINK_RELEASE_CHANGE_WINDOW_DAYS` (default 7 → 14).

## Considered alternatives

- **Trust the story's claimed date (invert the exact-match guard: fire when `claimed_date !=
  film.release_date`).** Rejected: it re-admits exactly the LLM date-extraction noise we are
  trying to remove — a mis-extracted date fires a false event. `claimed_date` is demoted to a
  hold-triage signal instead.
- **Terminal-reject uncorroborated stories (no hold).** Rejected: since the trades lead TMDB,
  this would silently drop most *genuine* date-change events — gutting the one condition the
  feature exists to serve.
- **Region-aware detection off `film_release_date`.** Deferred: no change history exists for that
  table; it is a separate build.
- **A special "allow immediately when `release_date IS NULL`" branch.** Rejected: a null→date is
  already a recorded change, so the unified rule honours it in the normal case; a standalone
  branch would only fire for a first date TMDB never confirms — precisely the LLM-trust hole this
  decision removes.

## Consequences

- Release-date events lag the news by up to one cluster cycle after TMDB corroborates. This is
  accepted: correctness (no false cards) is worth a short delay.
- A genuine date change that TMDB **never** reflects (e.g. a film pushed out of the discover
  window) produces no event — the held story expires as uncorroborated. Accepted.
- Regional-only date moves no longer card until region-aware detection is built.
- No schema migration: the rule reads the existing `film_field_change`, `link_note` is free text,
  and `claimed_date` is used transiently (still not persisted on `Event`).
