# The claim ledger is `news.event`, not a new table; state is never held, events always are

**Status:** accepted
**Relates to:** ADR-0014 (catalog-sourced events), ADR-0016 (publication axis), NEU-1200/1205

## Context

The consumer-pivot spec (`docs/specs/backlotter-consumer-pivot-spec.md`, §3.1) sketches a
`claim` table that everything ingested — trade stories and TMDB changes alike — lands in, with
`first_detected_at`, `published_at`, a `status` of `pending | published | superseded | discarded`,
and `superseded_by`. Alerting is a separate decision over that table.

`news.event` already carries most of that shape, built up over ADR-0014 and ADR-0016:

| Spec `claim` column | Existing |
|---|---|
| `source_type` (trade \| tmdb_change) | `Event.provenance` (story \| catalog) |
| `confidence` | `Event.confidence` |
| `first_detected_at` | `Event.occurred_at` (the change's own timestamp) |
| `published_at` | `Event.created_at` — already the axis every feed surface keys on |
| `subject` | `Event.subject_key` (names today; person ids after resolution) |
| raw observation | `catalog.film_credit_change`, `film_release_date_change`, `film_field_change`, `news.story` |

What is missing is `status`, `superseded_by`, and an explicit hold before publication for credit
attachments — removals already have one (NEU-1205's forward-dwell gate).

Three options were on the table: extend `news.event`; add a `news.claim` table beneath events
with events as projections over published claims; or replace the event model with the spec's
shape outright.

## Decision

**`news.event` is the claim ledger.** It gains `status` (`published | superseded`) and
`superseded_by`; nothing else about its shape changes. The raw observation tables stay the
"detected" layer, and `created_at` stays the publication axis (ADR-0016 is unchanged and is the
mechanism by which INV-2 — publish date sorts, detect date is metadata — holds).

**Quarantine is a hold before carding, never a hidden row.** A credit attachment is carded only
once its `film_credit_change.changed_at` is older than the quarantine window and the credit is
still present; a change reverted inside the window never becomes an event. This is the same
shape as the removal-side forward-dwell gate, so `created_at` is by construction the moment of
publication and no `pending` rows ever exist to leak into a feed.

**State is never held.** `catalog.film_credit` remains a delete-and-rebuild mirror of TMDB that
publishes immediately (INV-1). The live cast list and the event log have opposite correctness
requirements — state self-corrects on the next sync leaving no residue; a published event
cannot be un-published without leaving two wrong rows — and they are never unified into one
"update" concept.

**Supersession marks the original; it never removes it.** When a removal cards for a person
whose attachment card is published, the attachment card is set `superseded` with
`superseded_by` pointing at the removal card. Both stay on every surface (the feed at their own
publication day, the film page at their occurrence day); the marker is the only change. A
retraction inside the quarantine window is not supersession — it is the absence of an event.

## Considered alternatives

- **A separate `news.claim` table with events as projections.** Cleaner ledger on paper, but a
  second store that link, cluster and summarize would all have to be re-plumbed onto, for no
  behaviour the extended event table cannot express. Rejected on cost.
- **Replace the event model with the spec's `claim` as written.** Discards the ADR-0014/0016
  machinery and every surface built on it. Rejected.
- **A persisted `pending` status for quarantined events.** Rejected: a pending row is one
  `WHERE` clause away from every feed surface, and its `created_at` would predate publication,
  breaking the ADR-0016 axis. The hold-before-carding shape has no such row.
- **Keep the paired-cards model with no `status` (NEU-1200's choice).** The later card is the
  correction and readers infer it. Rejected for the consumer audience: the original card carries
  no signal it was ever retracted, which the spec names as the thing most likely to make a
  civilian distrust the app.

## Consequences

- No new store; the migration is two columns and one index on `news.event`.
- `occurred_at` must stay stored on every event (it is needed for window tuning, "first seen on"
  lines, and who-reported-first comparisons) and must never order a feed. ADR-0016 already says
  so; this ADR restates it as a consequence of the ledger choice.
- The quarantine window is a setting with a data-derived default, not a guess; its tuning is a
  spike against `film_credit_change`, which has been logging since 2026-08.
- Genuine sequential changes (joined in March, left in June) publish as two events, the first
  marked superseded by the second. The window suppresses edits that were never true, and only
  those.
