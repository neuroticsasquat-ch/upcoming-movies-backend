# NEU-1436 — Admission is an attachment: the baseline exception for followed people, studios and franchises

**Target repo:** upcoming-movies-backend

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1436
**Story:** NEU-1423 — The catalog observes every attachment an entity follow will deliver
**Milestone:** M2 — The signals (shared contracts live on the milestone)
**Blocked by:** NEU-1432 (binary follows, `followed_people()`), NEU-1433 (`film_company_change`,
`film.companies_observed_at`), NEU-1434 (`collection_id` in `TRACKED_FIELDS`,
`collection_attached`). None had merged when this spec was written (2026-09-21); where it names
their seams it names what their tickets promise, and the implementer must read the merged code
first and adjust the call sites, not the rule.
**Decisions:** EF-4 in `bl-entity-follows-project-spec.md`; ADR-0019 decision 4; the one
exception to ADR-0014's "first observation is a baseline, never an event" (spec §5.3)
**Ground truth read:** `ingest/tmdb/upsert.py`, `ingest/tmdb/credit_history.py`,
`ingest/sweep/credit_events.py`, `ingest/sweep/field_events.py`, `ingest/sweep/admission.py`,
`ingest/sweep/enumerate_phase.py`, `app/follow_queries.py`, `catalog/models.py`
(`FILM_FIELD_CHANGE_DENYLIST`, the `film_field_change` trigger), migration `957421e2651e`

---

## What to build and why

A film's first observation is a baseline. `load_recorded_credits` returns `None` when
`film.credits_observed_at` is NULL, `diff_recorded_credits(previous=None)` returns nothing, and
the `film_field_change` trigger is `BEFORE UPDATE` so an inserted row writes no history. Without
that rule, admitting the catalog would have carded tens of thousands of false attachments.

Under ADR-0019 the rule swallows the one attachment that matters most. Somebody follows a
director to hear about the director's *next* film. That film enters the catalog through the
sweep's `followed` tranche, or a seed tranche, or a story, **with the director already on it**,
and the baseline rule says nothing happened. The same is true of a followed studio on a film's
first company row, and a followed franchise on a film inserted with a `collection_id`.

EF-4 adds the one exception: on a film's first observation, every credit, company row and
collection held by an entity **somebody follows at that moment** is recorded as `added`. Nothing
else on the new film is. Those rows then go through the ordinary quarantine, burst grouping,
sanity holds and carding, and become ordinary `casting` / `crew_attached` / `company_attached` /
`collection_attached` cards with `occurred_at` = the observation. This ticket touches the
history writers only; every reader downstream is unchanged.

### Decided in the planning session (2026-09-21)

- **The followed backlog enters silently.** `SWEEP_ADMIT_FOLLOWED` flips on as NEU-1432's deploy
  step, and at least one sweep runs before this ticket deploys, so every followed person's
  existing slate is admitted as a baseline. Only films admitted *after* this ticket lands card
  their followed entities. No age cap, no backfill of cards. (D-1436.7.)
- **NEU-1433's migration backfills `companies_observed_at = now()`** on every existing film, so
  the first company diff after its deploy is a diff, not a first observation. Without that this
  ticket would card `company_attached` for every followed studio across the catalog on the next
  refresh. Recorded on NEU-1433 as a constraint; this ticket keys on the marker being NULL and
  does nothing defensive. (D-1436.3.)
- **The collection admission is a synthetic `film_field_change` row** (`field='collection_id'`,
  `NULL → id`), inserted by the admission path only when the film row was inserted *and* somebody
  follows the collection. NEU-1434's reader, quarantine and presence check then treat it like any
  update-driven row. Rejected: carding directly from admission (two carding rules for one beat)
  and a third history table (forks the path NEU-1434 chose). (D-1436.4.)
- **`credits_observed_at` is backfilled in this ticket.** Its migration never stamped existing
  films, so a film not re-read since 2026-08-11 still has NULL and would be "newly admitted" the
  next time TMDB returns it. This ticket's migration stamps `now()` where the marker is NULL and
  the film holds at least one `film_credit` row. The cost is accepted: such a film's next read
  is a diff against stale credits, and a director who genuinely attached since August cards as
  if attached now. (D-1436.6.)

---

## Design

### D-1436.1 — The exception is a separate function, not a change to the diff

`diff_recorded_credits(previous=None, …)` keeps returning `[]`. The baseline rule stays
structurally intact and every test that pins it stays green. The exception is a new pure
function in `ingest/tmdb/credit_history.py`:

```python
def admission_attachments(
    current: Collection[RecordedCredit], *, followed: Collection[int]
) -> list[CreditChange]:
    """The `added` rows a first observation writes: every recorded credit whose person is
    followed. Everything else in `current` is the baseline. Stable order, as `_ordered`."""
```

`_upsert_credits` chooses at the one point it already knows which case it is in:

```python
if previous_recorded_credits is None:
    changes = admission_attachments(current, followed=followed)
else:
    changes = diff_recorded_credits(previous=previous_recorded_credits, current=current)
await record_credit_changes(session, film_id, changes)
await mark_credits_observed(session, film_id)
```

`followed` is the set `_upsert_credits` already loads once per film (after NEU-1432,
`followed_people()`; no entitlement filter, as the recorded grade). Both sides of every diff,
and the admission set, read that one value. That is D-49's rule and what makes "a follow
created one ingest later" a no-op: on the second ingest `previous` is a set, the person is in
`followed` on both sides, the credit is present on both sides, and the diff is empty.

Every credit of a followed person is written: seed-grade and non-seed, cast and each crew job,
one `RecordedCredit` each. A followed writer-director on a new film writes two `added` rows and
cards once (`crew_attached` groups per film, type and pass). A non-followed director on the same
film writes nothing.

### D-1436.2 — Companies: the same shape on NEU-1433's seam

NEU-1433 diffs `film_production_company` in `_rebuild_joins` (or a sibling called from
`upsert_film`), takes the previous set as an argument, and writes no rows when
`film.companies_observed_at` is NULL. This ticket adds the mirror of D-1436.1 there: when the
previous set is `None`, write `film_company_change(change='added')` for every incoming company
id in `followed_companies()`; otherwise the ordinary diff. The marker is set after, write-once,
exactly as `mark_credits_observed` is.

There is no recorded grade for companies (every company change is recorded), so `followed` is
read only for the admission case. Read it once per `upsert_film`, at the same point the person
set is read, so a company follow made mid-ingest cannot split the answer.

### D-1436.3 — Keying on the markers, and the constraint on NEU-1433

"First observation" for credits is `credits_observed_at IS NULL`; for companies it is
`companies_observed_at IS NULL`. Neither is inferred from the join tables being empty (the
`credit_history` module docstring explains why: a speculative entry admitted with no credits
would be baseline again on its next read).

Keying on a marker only works if the marker is true for every film already in the catalog.
**NEU-1433's migration must stamp `companies_observed_at = now()` on every existing film row**
(posted on that ticket 2026-09-21). If NEU-1433 has merged without it when this ticket is
implemented, this ticket's migration does the stamp instead, beside D-1436.6's. Either way, the
implementer verifies on the merged migration before writing the company half.

### D-1436.4 — Collections: a synthetic `film_field_change` row on insert

`film_field_change_trg` is `BEFORE UPDATE`. A film inserted with a `collection_id` writes no
history, so NEU-1434's `field_events` reader never learns of it. The admission path writes the
row the trigger would have written on an update:

```python
FilmFieldChange(film_id=film_id, field="collection_id", old_value=None, new_value=collection_id)
```

with `changed_at` defaulting to `now()`. Written **only when both hold**: the upsert inserted
the film row, and `collection_id` is in `followed_franchises()`. A film updated into a
collection is the trigger's business already; a film inserted into an unfollowed collection is
a baseline.

"The upsert inserted the film row" comes from the pre-select `_slug_for_insert` already makes:
`_upsert_film_row` returns `(film_id, inserted)` (or `_slug_for_insert` reports whether it found
a row and `upsert_film` threads the flag). Do not detect insertion from `credits_observed_at`
or `companies_observed_at`: those are per-payload-section markers with their own backfill
histories, and the collection has no marker of its own.

The row is indistinguishable from a trigger-written one, on purpose. NEU-1434's reader maps
`NULL → id` to `collection_attached`, quarantines it, and checks at publication that the film
still holds the collection. A followed franchise's admission therefore cards on the same clock
as every other collection attachment. The row also makes the film look active to
`dormant_film_clause`, which a newly admitted film is anyway.

Note the vocabulary: the follow row's `entity_type` is `franchise` (`FOLLOW_ENTITY_TYPES`), its
`entity_id` is the TMDB collection id, and the catalog column is `collection_id`. The glossary
calls the product concept a franchise and the TMDB object a collection; the code keeps both.

### D-1436.5 — Two new system-wide follow queries

`app/follow_queries.py` gains `followed_companies()` and `followed_franchises()` beside
NEU-1432's `followed_people()`: `SELECT DISTINCT CAST(entity_id AS integer)` for every live
follow of that `entity_type`, digit-guarded by `_INT_ID_PATTERN`, **no entitlement filter** and
no user, for the reason `followed_people()` gives (a lapsed user's follow costs one history row
and is exactly what should be there when they return). The ingest path loading them has the
precedent `load_followed_person_ids` cites.

Three separate builders rather than one parameterised by type: each is one line, the call sites
are different modules, and the person one is already named.

### D-1436.6 — Migration: backfill `credits_observed_at`

One data-only migration, no schema change (the parity test in `test_migrations.py` is
unaffected):

```sql
UPDATE catalog.film SET credits_observed_at = now()
WHERE credits_observed_at IS NULL
  AND EXISTS (SELECT 1 FROM catalog.film_credit c WHERE c.film_id = film.id);
```

`credits_observed_at` is in `FILM_FIELD_CHANGE_DENYLIST`, so the update writes no history and
wakes no dormant film. Films with NULL and no credit rows are left NULL: they were admitted with
an empty payload and never observed, and their first credits are a baseline still. Downgrade is
a no-op (there is nothing to restore).

### D-1436.7 — Deploy order

1. NEU-1432 merges and `SWEEP_ADMIT_FOLLOWED=true` is set in Coolify.
2. At least one sweep completes with `followed×N` on `/admin/runs`. The followed backlog is now
   in the catalog as baselines.
3. NEU-1433 and NEU-1434 merge (their migrations run; NEU-1433's stamps every film).
4. This ticket merges and its migration runs.

The PR body repeats steps 1 to 3 as preconditions. If step 2 is skipped, the first sweep after
this ticket deploys cards every followed person's whole upcoming slate at once; the per-person
burst hold (D-8) would catch the prolific ones and need admin release, and the rest would land as
a day-one flood. That is the outcome the planning session chose against.

### D-1436.8 — What the downstream inherits without change

- **Quarantine** (`SWEEP_CREDIT_QUARANTINE_HOURS`, presence check at publication): an admission
  `added` row is judged like any other. If the user unfollows before the window closes, the
  credit is no longer recorded-grade, `present_recorded_credits` drops it and the card never
  publishes. That is existing behaviour for every followed-person credit and is accepted.
- **Burst grouping** (D-7): one `casting` and one `crew_attached` per film per pass, naming
  every eligible person; companies and collections group under NEU-1433 / NEU-1434's rules.
- **Sanity holds** (D-8): a followed producer whose fifteen films are admitted on one day trips
  the burst hold on the person. Correct: it is the shape the hold exists for, and an admin can
  release it.
- **Detachments**: an admission-carded credit that later leaves cards `credit_removed`, because
  the removal gate only asks for a prior visible attachment card and now there is one.
- **D-5 short-circuit / D-6 promotion**: a story that names the attachment before quarantine
  clears stamps the row `carded_by_event_id`, and the backlog reader skips it. Unchanged.
- **Confidence and provenance**: `rumored`, `catalog`, as every credit attachment.

### D-1436.9 — Documents

- `CONTEXT.md`, **First observation**: "emits no events" gains "except for entities somebody
  follows at that moment (EF-4, NEU-1436)". **Recorded grade** and **Attachment** already say
  it; check the three agree.
- `docs/adr/0014-catalog-sourced-events.md`: an **Amendment — NEU-1436** block under the
  baseline rule, in the style of the NEU-1200 amendment, stating the exception, that it is
  keyed on the follow set read at the observation, and that the collection case is a synthetic
  `film_field_change` row because the trigger is `BEFORE UPDATE`.
- `credit_history.py` module docstring: the "first observation is a baseline" paragraph gains
  the exception and the reason it lives in `admission_attachments` rather than in the diff.
- `docs/specs/bl-entity-follows-project-spec.md` §6 M2 shared contracts: add
  `followed_companies()` / `followed_franchises()` and NEU-1433's backfill.

### What does not change

- `diff_recorded_credits`, `load_recorded_credits`, `mark_credits_observed`, `credit_events.py`,
  `field_events.py`'s reader, the event types, `subject_key` tokens, summaries, the notify and
  digest passes.
- Which films are admitted, and by which tranche. `admission.py` is unchanged; the "followed
  tranche is the main path" in the ticket is a statement about where the rows will come from,
  not a hook.
- No entitlement filter anywhere on this path.

---

## Acceptance criteria

### `ingest/tmdb/credit_history.py`, `upsert.py`

- New film, followed director in the payload → one `film_credit_change(change='added')` for that
  credit, `changed_at` = the observation; the non-followed cast on the same film writes nothing;
  `credits_observed_at` is set.
- Same film and payload, nobody follows anyone → zero rows (the existing
  `test_first_credit_ingest_writes_no_change_rows` still passes).
- New film with a followed writer-director → two `added` rows (one per job).
- Film observed on ingest 1 with nobody following; follow created; ingest 2 with identical
  credits → zero rows. Pinned as its own test, named for D-49.
- Film admitted with an empty credits payload while its director is followed → nothing; a later
  ingest bringing the director → one `added` (the existing observed-marker rule).
- `diff_recorded_credits(previous=None, current=…)` still returns `[]` for any input.

### `ingest/tmdb/upsert.py` — companies (on NEU-1433's merged seam)

- New film with a followed production company → one `film_company_change(change='added')`;
  non-followed companies on the same film write nothing; `companies_observed_at` is set.
- Same, nobody follows the company → zero rows.
- Follow created after admission, next ingest identical → zero rows.

### `ingest/tmdb/upsert.py` — collections

- Film **inserted** with a followed collection → one `film_field_change` row
  (`collection_id`, `old_value` NULL, `new_value` = the id).
- Film inserted with an unfollowed collection → no row.
- Film inserted with no collection → no row.
- Existing film **updated** into a followed collection → exactly one row, written by the trigger,
  not two.

### `ingest/sweep` — end to end

- Integration test through `run_credit_attachment_events`: admit a film with a followed director,
  advance the clock past `SWEEP_CREDIT_QUARANTINE_HOURS`, run the phase → one `crew_attached`
  event, `provenance='catalog'`, `confidence='rumored'`, `occurred_at` = the row's `changed_at`,
  `subject_key` naming the person.
- Same, with the person unfollowed before the clock advances → no event.
- Collection variant through NEU-1434's phase → one `collection_attached` after quarantine.
- Company variant through NEU-1433's phase → one `company_attached` after quarantine.

### `app/follow_queries.py`

- `followed_companies()` and `followed_franchises()` return distinct integer ids across users,
  include a lapsed user's follows, and skip a non-numeric `entity_id`.

### Migration

- `credits_observed_at` is stamped where NULL and credit rows exist, left NULL where no credit
  rows exist; the update writes no `film_field_change` row (assert on a film in the
  `test_migrations.py` scratch DB or a dedicated integration test).
- If NEU-1433 merged without stamping `companies_observed_at`, the same migration stamps it on
  every existing film.

### Tooling

- `task format`, then `task test && task lint && task typecheck` inside the container.
- PR body lists D-1436.7's preconditions and confirms the sweep ran with `followed×N` before
  merge.

---

## Out of scope / deferred

- Carding the followed backlog that entered before this ticket (chosen against, D-1436.7).
  A user who wants a followed person's existing slate finds it on the person page (M1) and the
  entity's cards section (M3).
- Any age cap or "TMDB entry age" rule on admission cards.
- Changing the delivery of these cards (M3's `entity_attachment_event_ids`), their push rule
  (EF-8, NEU-1438) or their story resolution (M4).
- Making company or collection attachments `confirmed` (spec §8 open item).
- A `film_collection_change` table. If the field-change trigger is ever rewritten to fire on
  insert, D-1436.4's synthetic row becomes a duplicate and must be removed with it; that
  coupling is the documented cost of reusing the trigger's table.
