# NEU-1446 — First-association predicate for all three kinds in one builder, wired into the timeline and the notify pass

**Target repo:** upcoming-movies-backend

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1446
**Story:** NEU-1426 — A story naming a studio or franchise joining or leaving a film reaches its followers once
**Milestone:** M4 — Studios and franchises in the news (shared contracts live on the milestone)
**Blocked by:** NEU-1445 (organisation extraction, `news.story_entity`, the four story-side
event types — see the constraints posted on it), NEU-1438 (per-type push whitelist, reads the
flip this ticket writes). Transitively on NEU-1437 (`first_association_clause`,
`entity_attachment_event_ids`), NEU-1433 (`film_company_change.carded_by_event_id`,
`company:<id>` tokens), NEU-1434 (`collection_id` field events, `collection:<id>` tokens),
NEU-1436 (`followed_companies()`, `followed_franchises()`). **None had merged when this spec was
written (2026-09-21)**; NEU-1436 and NEU-1437 were marked Done in Linear by the docs PR #352 and
were reopened in this session. Where this spec names their seams it names what their specs
promise; the implementer reads the merged code first and adjusts call sites, not the rule.
**Decisions:** EF-8, EF-10, EF-11, EF-12, EF-13 in `bl-entity-follows-project-spec.md`; D-2, D-5,
D-6 in `bl-consumer-pivot-project-spec.md`; ADR-0019 decisions 2 and 6.
**Ground truth read:** `app/follow_queries.py`, `news/credit_confirm.py` (both stamp
directions, `_confirmed_names`, the "M4 seam" paragraph), `ingest/sweep/credit_events.py`
(`load_attachment_backlog`, `supersede_prior_attachment_cards`, the `stamp_prior_story_cards`
call), `ingest/sweep/field_events.py` (`_already_carded`, `load_change_backlog`),
`link/cluster.py` (`_VALID_TYPES`, `_record_mentions`, `features={"event_type": …}`),
`link/resolve/pipeline.py` (`_write_decision`, `resolved_at` as the backlog marker),
`news/models.py` (`Event.updated_at`, `StoryPerson`, `RESOLVED_MENTION_PATHS`),
`catalog/models.py` (`FilmCreditChange.carded_by_event_id`, `FilmFieldChange`),
`catalog/queries.py::present_recorded_credits`, `app/services/notify_service.py`.

---

## What to build and why

NEU-1437 states EF-13 once, for people: a resolved `story_person` mention of a followed person
reaches that person's followers only as the first association or first detachment with the
film, from one builder, `first_association_clause`, that the timeline and the notify pass both
call. This ticket extends that builder to `news.story_entity` (NEU-1445) so a studio or
franchise follower sees a story-backed attachment once, and it closes the two loops the person
arm already has and the organisation arms need:

- **D-5's stamp**, so a catalog company or collection change that a story already carded is
  stamped `carded_by_event_id` and never raises a second card.
- **Confirmation**, so a rumored story card becomes `confirmed` when the catalog change it
  predicted clears quarantine — the flip that EF-10 promises and NEU-1438's push waits on.
  Nothing writes that flip today, for any kind.

### Decided in the planning session (2026-09-21)

- **The story vocabulary gains the four organisation types.** `company_attached`,
  `company_removed`, `collection_attached`, `collection_removed` join the cluster stage's
  event vocabulary, and an organisation mention's `event_type` uses the same values, exactly
  as `casting` is both a card type and a mention type for people. Lands in NEU-1445 (posted
  there as a constraint); this ticket matches card type and mention type symmetrically with
  the person arm. Rejected: a mention-level `attached|detached` relation with the card left
  generic (a company follower would see an `announced` card as "the attachment", and `other`
  cards are hidden); attach types only (drops the ticket's story-detach case). (D-1446.1)
- **The collection stamp is a column on `film_field_change`.** `carded_by_event_id`, nullable
  FK to `news.event`, SET NULL, indexed, in this ticket's migration. One stamp shape for all
  three kinds. Rejected: a time-based carve-out for collections only (diverges, and an
  unrelated later re-file would be attributed to the card); a sidecar table (a second join
  everywhere the stamp is read). (D-1446.2)
- **The stamp is where confirmation lives, for all three kinds, in this ticket.**
  `credit_confirm.py` is generalised rather than mirrored, and the person path gains the flip
  with it. Rejected: organisations only (two kinds behave differently); leaving it to NEU-1438
  (whose ticket assumes the flip exists). (D-1446.3)
- **The flip happens at quarantine clear, by a sweep step, not at stamp time.** The backward
  stamp runs the moment TMDB's change is observed, before the 72-hour window, and a stamped
  row is dropped from the carding backlog so nothing revisits it. A new step reads stamped
  rows older than the window whose card is still rumored, re-checks presence, and flips the
  card. A reverted change never confirms a card. (D-1446.4)
- **A story-formed detach card supersedes the prior attach card at confirmation.** As a rumor
  it supersedes nothing; when it confirms — at formation if the story was `confirmed`, on D-6
  promotion, or in the flip step — the entity's prior published attach card on that film is
  marked `superseded_by` the detach card, exactly as a catalog detach would have done (D-2).
  Rejected: superseding when the card forms (a wrong trade hides a true attachment); never
  from a story (the attach card stays published beside a confirmed contradiction). (D-1446.5)
- **Organisations stamp by resolved id, and only in the backward direction.** The forward
  direction (`stamp_story_confirmed_changes`, called from the cluster stage) matches names in
  `subject_key`; an organisation's id is not known until the resolve stage runs, later, on its
  own backlog. Rather than hook `_write_decision`, the organisation arms rely on the backward
  direction alone: the sweep's loader stamps a pending company or collection change against
  any published story card whose resolved `story_entity` names the entity, whichever came
  first. The loader runs before carding, so no duplicate is possible; the only cost is that a
  change TMDB had first waits one sweep for its stamp, which is also when it would have
  waited anyway. The person forward direction is untouched (the M4 seam in
  `credit_confirm.py`'s docstring stays a seam). (D-1446.6)

---

## Design

### D-1446.1 — The story vocabulary (NEU-1445's half, restated so this spec is self-contained)

`link/cluster.py::_VALID_TYPES` gains the four organisation types; `_STALE_EVENT_TYPES` gains
the two attach types (a studio boarding a film that is already in post is stale the way a
casting is). The cluster prompt's event `type` list and the organisation mention's
`event_type` share the vocabulary. `story_entity.features->>'event_type'` is where the
mention's type lives, mirroring `story_person`.

This ticket adds two constants beside NEU-1437's, in `app/follow_queries.py` (or wherever
NEU-1437 put `STORY_ATTACH_MENTION_TYPES`):

```python
STORY_ATTACH_MENTION_TYPES = frozenset({"casting", "company_attached", "collection_attached"})
STORY_DETACH_MENTION_TYPES = frozenset({"company_removed", "collection_removed"})
```

The detach set stops being empty. It still has no person type: the story vocabulary has no
`credit_removed`, so the person detach arm keeps selecting nothing, and the test that pinned
the set empty becomes a test that pins it to these two. Per-kind pairs, spelled once:

| kind (`story_entity.kind`) | follow `entity_type` | attach card / mention type | detach card / mention type | current attachment | change table |
|---|---|---|---|---|---|
| person (`story_person`) | `person` | `casting`, `crew_attached` (mention: `casting`) | `credit_removed` (mention: none) | `film_credit(film, person)` | `film_credit_change` |
| `company` | `company` | `company_attached` | `company_removed` | `film_production_company(film, company)` | `film_company_change` |
| `collection` | `franchise` | `collection_attached` | `collection_removed` | `film.collection_id = id` | `film_field_change(field = 'collection_id')` |

### D-1446.2 — `first_association_clause` grows two arms inside the one builder

NEU-1437's D-1437.5 predicate is kept as written for people. The builder becomes a
`union_all` of three kind-arms, each an attach half and a detach half, wrapped in one
`select(distinct)`; the signature (`first_association_clause(*, user_id)`, returns
`Select[tuple[UUID]]`, no session, `correlate(None)`) does not change, and neither does the
call from `entity_attachment_event_ids`. The notify pass reaches it through that builder as
before; nothing in `notify_service` is edited by this ticket.

For a published event `E` on film `F`, a story `S` attached to `E` (`event_story`), and a
resolved mention `M` (`story_entity`, `path IN RESOLVED_MENTION_PATHS`, `kind = K`,
`entity_id = X`) where `X` is a company or franchise the user follows
(`followed_tmdb_ids(user_id, "company")` / `(…, "franchise")`, digit-guarded):

**Attach arm** — all of:

1. `E.event_type` is `K`'s attach card type and `M.features->>'event_type'` is the same value.
2. **No earlier published attach card for `X` on `F`:** no published event on `F` of `K`'s
   attach type with `created_at < E.created_at` that names `X` — by its `company:<id>` /
   `collection:<id>` token (the catalog card, NEU-1433 / NEU-1434) *or* by a resolved
   `story_entity` mention with the attach type on one of its stories (a story card). The two
   mechanisms are the two `entity_attachment_event_ids` already uses, so "a card names `X`"
   means one thing throughout the module.
3. **No current attachment not attributable to `E`:** `NOT EXISTS` the current row (the table's
   column in the matrix above), **or** `EXISTS` an `added` change row for `(F, X)` stamped
   `carded_by_event_id = E.id`. For collections the change row is
   `film_field_change(film_id = F, field = 'collection_id', new_value = X, carded_by_event_id
   = E.id)`. The carve-out is D-1437.5's, for the same reason: a change the catalog observed
   after the story and that D-5 stamped as published by `E` is `E`'s own confirmation. A
   baseline row (no change row) still blocks; an unstamped later change blocks too, and the
   sweep raises its own catalog card, which the token branch selects.

**Detach arm** — all of:

1. `E.event_type` is `K`'s detach card type and `M.features->>'event_type'` is the same value.
2. No published detach card for `X` on `F` (token or resolved mention, as above) created after
   the latest published attach card for `X` on `F` and before `E`. With no attach card at all
   the arm still selects: a story reporting a studio's exit from a film we only ever knew it
   on as a baseline is the first detachment we have heard of.

Like the person arm, the organisation arms do **not** re-check the mention against
`E.subject_key`; story cards carry no organisation token (resolution runs after clustering).
A `company_attached` story card about studio A whose story also names studio B with
`event_type = company_attached` reaches B's followers if B holds no company row on the film
and has no attach card — the same "first association in our data" reading D-1437.5 accepted.

Mutes and the `only=` seam are applied where NEU-1437 put them; nothing per arm.

### D-1446.3 — The stamp generalised: `news/attachment_confirm.py`

`news/credit_confirm.py` is renamed to `news/attachment_confirm.py` (the module docstring's
"M4 seam" paragraph is replaced by this design; keep the rest) and grows from one kind to
three. The person functions keep their names and behaviour; the new surface is:

```python
async def stamp_prior_story_cards(session, *, since, within_days) -> int
    # Backward direction, all three kinds. People by normalized name as today; companies and
    # collections by resolved id: a pending `film_company_change(added)` / `film_field_change
    # (collection_id, NULL→id)` row is stamped with the *earliest* published story card on its
    # film of the kind's attach type whose `story_entity` resolves (RESOLVED_MENTION_PATHS,
    # features->>'event_type' = the attach type) to that company / collection, with the same
    # `occurred_at >= changed_at - within_days` bound. The same for `removed` / `id→NULL` rows
    # against the kind's detach card type. Held rows are read past (D-8), as today.

async def confirm_stamped_cards(session, *, now, quarantine: timedelta) -> int
    # D-1446.4. For every change row of any kind with `carded_by_event_id` set, `changed_at
    # <= now - quarantine`, whose card is `provenance = 'story'` and `confidence = 'rumored'`:
    # re-check presence (the credit under its recorded role via `present_recorded_credits`;
    # the company row; `film.collection_id`) — for an `added` row the attachment must still
    # hold, for a `removed` row it must still be gone. If it holds, set `confidence =
    # 'confirmed'` and `updated_at = now` on the card, and for a detach card call
    # `supersede_prior_attachment_cards` (D-1446.5). If it does not hold, do nothing: the row
    # stays stamped, the card stays rumored, and the next pass looks again. Returns cards
    # flipped. Idempotent: a confirmed card is never selected.
```

Where the loaders stamp: `credit_events.py` already calls `stamp_prior_story_cards` before
`load_attachment_backlog` reads `carded_by_event_id IS NULL`; NEU-1433's company carder and
`field_events.py::load_change_backlog` (for `collection_id` rows) get the same call and the
same `IS NULL` term, so a stamped row is never carded twice. `_already_carded` in
`field_events.py` stays as it is for `status`; the `collection_id` path reads the stamp.

`confirm_stamped_cards` runs as one step at the end of the sweep, after every carder, with
`quarantine = SWEEP_CREDIT_QUARANTINE_HOURS` (the one quarantine setting M2 reuses for all
three kinds). Its counts land on the run row beside `story_published` (`cards_confirmed`).

The story-side forward direction (`stamp_story_confirmed_changes`) is unchanged and person-only
(D-1446.6).

### D-1446.4 — Supersession from a story detach card (D-1446.5)

`supersede_prior_attachment_cards(session, *, removal)` in `credit_events.py` today supersedes
the person's attach cards named by `removal.subject_key`. It is widened to take the entity
explicitly — `(kind, entity_id | normalized name)` — so a story detach card, which carries no
organisation token, can name what it supersedes; the catalog callers pass what they pass today.
Three call sites for story detach cards:

- **Formed `confirmed`** (the cluster stage created a `company_removed` / `collection_removed`
  card with `confidence = 'confirmed'`): supersede on creation. Resolution has not run yet, so
  this is done by the resolve stage when the mention resolves (`_write_decision` for
  `story_entity`, path accepted or tiebreak) and the card it belongs to is confirmed — the
  one place the id and the card meet.
- **Promoted by D-6** (a confirmed story attaches to a rumored story detach card): same hook,
  same condition, on the promoting story's mention.
- **Flipped by `confirm_stamped_cards`**: the step calls it directly.

A person story detach card cannot exist (no story vocabulary), so the person case has no call
site; do not add one.

### D-1446.5 — Migration

One revision: `catalog.film_field_change.carded_by_event_id` (UUID NULL, FK `news.event.id`
ON DELETE SET NULL, named `fk_film_field_change_carded_by_event`, index
`ix_film_field_change_carded_by`). No backfill: no story card has ever carded a collection
change. The migration parity test compares constraint names, so the model and the revision
must agree. If NEU-1433 merged without an index on its stamp column, add it here.

### D-1446.6 — What the notify pass inherits

Nothing in this ticket edits `notify_service.py`. The one assertion it adds is the ticket's:
a test that `entity_attachment_event_ids` and, through it, `digest_event_ids` /
`alert_event_ids` reach `first_association_clause` (NEU-1437 pins this; extend the test so the
organisation arms are what is observed). The push consequences:

- Story card rumored → digest and timeline only (EF-10); NEU-1438's provenance rule never
  pushes a `provenance = 'story'` rumored card.
- Catalog change arrives, is stamped, clears quarantine, `confirm_stamped_cards` flips the
  card and bumps `updated_at` → NEU-1438's window (keyed on `updated_at` for attach and detach
  types, posted there as a constraint) queues one push. If NEU-1438 has not merged, the "one
  push" test is written against `updated_at` and marked to enable on its merge.
- Story card formed `confirmed` → EF-11, pushes on the story card; the later catalog change is
  stamped and raises nothing.

### D-1446.7 — Documents

- `CONTEXT.md` **Attachment**: a story can report one before the catalog does; the card is a
  rumor until the catalog confirms it, and a confirmed story detachment supersedes the
  attachment it contradicts. **Quarantine**: also the wait before a story card confirms.
  Done in this planning session.
- `docs/specs/bl-entity-follows-project-spec.md` §6 M4 shared contracts: the four story
  types, the field-change stamp, the flip step. Done in this planning session.
- `credit_events.py` module docstring and `attachment_confirm.py` docstring: the three-kind
  shape and D-1446.4 / D-1446.6.

### What does not change

- `entity_attachment_event_ids`' token, name and `canceled` branches, mutes, `only=`.
- `deliverable_events()`, `PUSH_WHITELIST` / the two push sets, the alert window (NEU-1438).
- `stamp_story_confirmed_changes` (person forward direction) and `_confirmed_names`.
- Catalog carding of company and collection changes (NEU-1433 / NEU-1434) except the stamp
  read in their loaders.
- `story_entity`'s schema, the resolve stage's scoring, `/admin/resolution` (NEU-1445 /
  NEU-1447).

---

## Acceptance criteria

### `first_association_clause` (`tests/integration/app/test_follow_queries.py`)

- Company: a story-formed `company_attached` card whose resolved `story_entity` names a
  followed company with no prior attach card and no `film_production_company` row → selected.
  Second story attaching to the same card → one id. Second story forming a second
  `company_attached` card on the same film → the later card is not selected.
- Company already holds a baseline row (no change row) → not selected. Company already has a
  published catalog `company_attached` card (token) → not selected.
- D-5 case: story card first, then `film_company_change(added)` stamped
  `carded_by_event_id = card` → still selected. Same, unstamped → not selected by this clause;
  the catalog card the sweep raises is selected by the token branch.
- Collection: the same five over `collection_attached`, `film.collection_id` and a
  `film_field_change(collection_id)` row stamped / unstamped; the follow row says `franchise`.
- Detach: a story-formed `company_removed` card for a followed company with a prior attach
  card and no detach since → selected; with a published `company_removed` between → not
  selected; with no attach card ever → selected. Same for `collection_removed`.
- Mention `path` unlinked / not_in_tmdb → never. Mention `event_type` `other` or null on an
  attach card (a possessive studio mention in an interview) → nothing. Mention on an
  `announced` card → nothing. `STORY_DETACH_MENTION_TYPES == {company_removed,
  collection_removed}` pinned; the person detach arm still selects nothing.
- The one-builder assertion: `entity_attachment_event_ids`, `digest_event_ids` and
  `alert_event_ids` reach the clause (monkeypatch it and observe the organisation ids in the
  call).

### `news/attachment_confirm.py` (`tests/integration/news/test_attachment_confirm.py`)

- Backward stamp: pending `film_company_change(added)` with an earlier published
  `company_attached` story card whose `story_entity` resolves to that company → stamped with
  the earliest such card; a mention with path unlinked → not stamped; a card outside
  `within_days` → not stamped; a held row → read past. Collection `NULL→id` the same over
  `film_field_change`. `removed` / `id→NULL` against the detach type. People unchanged.
- `confirm_stamped_cards`: stamped row younger than quarantine → nothing; older and
  attachment present → card `confirmed`, `updated_at` bumped; older and reverted → nothing,
  and it flips on a later pass once present again; already confirmed → not selected; a
  stamped detach row → the card flips and the prior attach card is `superseded_by` it.
  All three kinds.
- The sweep's loaders (`credit_events`, the company carder, `field_events` for
  `collection_id`) skip stamped rows: story card + stamped change + a sweep pass → no second
  card.

### End to end (`tests/integration/ingest/sweep/…`, `tests/integration/link/test_cluster.py`)

- First association via story then TMDB → one card, one `confirmed` flip after quarantine,
  and (with NEU-1438) one push; via TMDB then story → the story attaches to the catalog card
  (D-6), no second card, no stamp needed.
- Story detach → rumored card, nothing superseded; after quarantine with the row gone →
  confirmed and the attach card superseded. A `confirmed` story detach → superseded when the
  mention resolves.
- Second outlet → attaches, no new row. A possessive studio mention with `event_type = other`
  → nothing.

### Migration

- `film_field_change.carded_by_event_id` exists with the named FK and index; parity test
  passes; autogenerate is clean.

### Tooling

- `task format`, then `task test && task lint && task typecheck` inside the container.
- PR body: the rename of `credit_confirm.py`; which blockers had merged and which seams were
  written against their specs; that NEU-1438 reads `updated_at`.

---

## Out of scope / deferred

- The four story types, the organisation prompt, `story_entity` itself (NEU-1445).
- The push decision, the two push sets, the `updated_at` window (NEU-1438).
- A story-side `credit_removed` type and the person detach arm's content.
- The person forward direction by resolved `story_person.person_id` (the M4 seam stays a seam;
  names still work for people).
- Widening `_already_carded` for `status` events; only `collection_id` reads the stamp.
- A `company:<id>` / `collection:<id>` token on story cards.
