# NEU-1437 — Timeline and digest read title films OR entity attachment events; first-association rule for person mentions

**Target repo:** upcoming-movies-backend

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1437
**Story:** NEU-1424 — An entity follow delivers its attachment stream; a title follow delivers everything
**Milestone:** M3 — The cutover (shared contracts live on the milestone)
**Blocked by:** NEU-1432 (binary follows, `followed_people()`, `credit_tier` deleted), NEU-1433
(`company_attached` / `company_removed`, `company:<id>` tokens), NEU-1434 (`collection_attached` /
`collection_removed`, `collection:<id>` tokens), NEU-1435 (`canceled`). None had merged when this
spec was written (2026-09-21). Where it names their seams it names what their tickets promise; the
implementer reads the merged code first and adjusts call sites, not the rule.
**Blocks:** NEU-1438 (per-type push whitelist), NEU-1439 (watchlist removal), NEU-1440
(`last_activity_at`, entity `/events`).
**Decisions:** EF-3, EF-7 (digest half), EF-10, EF-13 in `bl-entity-follows-project-spec.md`;
ADR-0019 decisions 2, 3 and 6.
**Ground truth read:** `app/follow_queries.py`, `public/service.py::get_timeline` /
`_feed_scope` / `get_feed_grouped`, `app/services/notify_service.py` (`deliverable_events`,
`alert_event_ids`, `digest_event_ids`, `decide_for_user`), `app/services/digest_sender.py`,
`news/models.py` (`Event`, `EventStory`, `StoryPerson`, `RESOLVED_MENTION_PATHS`),
`news/subject_key.py`, `news/credit_confirm.py`, `link/cluster.py` (story card `subject_key`,
mention `event_type`), `catalog/models.py::FilmCreditChange`.

---

## What to build and why

Today the timeline is D-11's two halves OR-ed: `followed_film_ids` (every film any follow
reaches, at the follow's coverage, in play) and `events_naming_followed_people` (every event
whose stories name a resolved followed person). A director follow therefore fans out to the
director's whole upcoming slate — trailers, dates, availability — and a mention in any story
about any of their films is a timeline row. The digest summarises the same set; the alert branch
reads a third set, the computed watchlist.

ADR-0019 replaces all of that with one clause. A **title** follow delivers everything about its
film. A **person, studio or franchise** follow delivers only the cards in which that entity is
attached to or detached from a film, plus the film's `canceled` card. The timeline's where-clause
becomes:

```
event.film_id IN title_follow_film_ids(user)  OR  event.id IN entity_attachment_event_ids(user)
```

and the digest and the notify pass read the same two builders. A story mention reaches an entity
follower only as the **first association** or **first detachment** of that entity with the film
(EF-13); an interview, a festival piece, or a second outlet reporting the same casting produces
nothing. This ticket does people; M4 (NEU-1446) extends the same builder to `story_entity`.

### Decided in the planning session (2026-09-21)

- **A followed person is matched to a card by normalized name, in SQL.** `Event.subject_key`
  carries `normalize_name(person.name)` tokens, never ids, and the sweep does not stamp
  `carded_by_event_id` on the rows it cards itself, so there is no id-level link from a catalog
  credit card to its person. The person branch joins `catalog.person` for the followed ids and
  compares a SQL spelling of `normalize_name` against the tokens. Rejected: stamping every
  sweep-carded change row (changes NEU-1371's meaning of the stamp, needs a backfill, pulls the
  sweep in); adding `person:<id>` tokens (contradicts M2's "people tokens unchanged"). (D-1437.3)
- **The first-association predicate carves out the event's own confirmation.** "No current
  credit" is read at query time, so without the carve-out a story card that published first (D-5)
  would drop off the timeline the day TMDB confirmed it. A credit whose `added` change row is
  stamped `carded_by_event_id = this event` does not count as a prior attachment. (D-1437.5)
- **The `confirmed` floor moves out of `deliverable_events()` into the alert branch.** Every
  catalog attach or detach card is `rumored`, so with the floor in the shared selector an entity
  follower's digest would carry nothing their follow delivers. EF-7: the digest carries
  everything the timeline carries. The alert branch keeps `confirmed` for every type as the
  interim rule until NEU-1438 applies EF-8's provenance clause. (D-1437.7)
- **This ticket rewires the event readers; NEU-1439 rewires the film readers.** Deleted here:
  `followed_film_ids`, `events_naming_followed_people`. Kept for NEU-1439 to delete with their
  last callers: `covered_film_ids`, `covering_follows`, `watchlist_film_ids`,
  `covered_by_any_user_clause`, `muted_film_ids`, `_not_muted`. Mutes are honoured inside the two
  new builders until NEU-1439 drops the table. (D-1437.8)

---

## Design

### D-1437.1 — Two builders, one clause

`app/follow_queries.py` gains:

```python
def title_follow_film_ids(user_id: UUID) -> Select[tuple[UUID]]:
    """`SELECT film.id` for every film this user follows by title, in any state, minus mutes."""

def entity_attachment_event_ids(user_id: UUID) -> Select[tuple[UUID]]:
    """`SELECT event.id` for every published attach, detach or `canceled` card this user's
    person, company and franchise follows deliver (EF-3, EF-13), minus muted films."""
```

Both are query builders in the module's existing shape: take a `user_id`, return a `Select`,
hold no session, `correlate(None)`, id casts in SELECT lists behind the digit / UUID guards. No
entitlement filter (the route gates; the notify pass records `suppressed`). No in-play or
alert-window term anywhere: a title follow reaches its film in any state (EF-14), and an entity
follow reaches events, not films, so there is nothing to bound.

`title_follow_film_ids` is `select(Film.id).where(Film.id.in_(followed_film_uuids(user_id)),
Film.id.not_in(muted_film_ids(user_id))).correlate(None)`. The mute term is the one thing NEU-1439
removes from it.

The timeline, `digest_event_ids` and `alert_event_ids` all spell the clause as
`or_(Event.film_id.in_(title_follow_film_ids(u)), Event.id.in_(entity_attachment_event_ids(u)))`.
`get_timeline` passes them as `film_filter` and `event_filter` to `get_feed_grouped`, whose
`_feed_scope` already OR-s the two grains; nothing in the feed changes.

### D-1437.2 — `entity_attachment_event_ids` is a UNION of four event selectors

```
entity_attachment_event_ids(user) =
    person_attachment_events(user)        -- D-1437.3
  ∪ company_attachment_events(user)       -- D-1437.4
  ∪ franchise_attachment_events(user)     -- D-1437.4
  ∪ canceled_for_attached_entities(user)  -- D-1437.6
  ∪ first_association_events(user)        -- D-1437.5, story-backed cards
minus events on muted films
```

Every branch reads `news.event` with `status = 'published'` and the event type set it owns; the
feed's own visibility terms (`visible_events()`, `region_visible()`, slug, summary join) stay in
the query the builder is dropped into, exactly as `events_naming_followed_people` left them. A
superseded attach card (D-2) is therefore never selected; its detach card is, and it names the
same entity.

Spelled as `union_all(...).subquery()` of `SELECT event.id` branches, wrapped in one
`select(distinct)`: an event can be reached by more than one branch (a company follow and a
person follow on the same `canceled` card; a catalog casting card matched by name and again by a
resolved mention of the story that promoted it) and the `IN` needs one row per event.

The private branch builders take an optional `only: tuple[str, str] | None` narrowing to one
`(entity_type, entity_id)` in `covered_film_ids`' shape. Nothing in this ticket passes it; it is
the seam NEU-1440's `last_activity_at` needs ("the newest card that reaches this user through
*this* follow") and costs one `if`.

### D-1437.3 — People: the name branch

Event types `casting`, `crew_attached`, `credit_removed` (`news.catalog_events.CREDIT_EVENT_TYPES`).
A card is about a followed person when one of its `subject_key` tokens equals the normalized name
of a person the user follows:

```python
followed_names = (
    select(sql_normalized_name(Person.name))
    .where(Person.id.in_(followed_tmdb_ids(user_id, "person")))
)
select(Event.id).where(
    Event.status == "published",
    Event.event_type.in_(CREDIT_EVENT_TYPES),
    Event.subject_key.overlap(array(followed_names)),   # subject_key && ARRAY(SELECT ...)
)
```

`sql_normalized_name(column)` is a new function in `news/subject_key.py`, beside
`normalize_name`, returning the SQL expression
`regexp_replace(btrim(lower(normalize(column, NFKC))), '\s+', ' ', 'g')` (Postgres 17;
`normalize()` needs a UTF-8 database, which `app_test` and production are). The two spellings are
pinned by a parity test over a fixture list of names (accents, NBSP, double spaces, mixed case,
Cyrillic, a name with `ß`). **Known divergence, accepted:** Python `casefold()` maps `ß` to
`ss`; SQL `lower()` does not. A person whose TMDB name contains such a character is missed by
this branch until the name is spelled the same on both sides; the resolved-mention branch still
catches story-backed cards. Document it on the function, not as a TODO. A renamed person
(subject_key written under the old name) is missed the same way; also accepted.

`credit_removed` cards name the removed person the same way the attach cards do (the sweep's
removal path writes `subject_key` from the same `normalize_name`); nothing special.

Why not `followed_people()` for the id set: that builder is system-wide (no user). The per-user
set is `followed_tmdb_ids(user_id, "person")`, which already exists.

### D-1437.4 — Companies and franchises: the token branch

Event types `company_attached`, `company_removed` (NEU-1433) and `collection_attached`,
`collection_removed` (NEU-1434), whose `subject_key` tokens are `company:<tmdb_id>` and
`collection:<tmdb_id>`. Exact match on the token:

```python
tokens = select(literal("company:") + Follow.entity_id).where(
    Follow.user_id == user_id, Follow.entity_type == "company",
    Follow.entity_id.regexp_match(_INT_ID_PATTERN),
)
select(Event.id).where(
    Event.status == "published",
    Event.event_type.in_(COMPANY_EVENT_TYPES),
    Event.subject_key.overlap(array(tokens)),
)
```

The franchise branch is the same over `entity_type = 'franchise'` and the `collection:` prefix
(the follow row says `franchise`, the catalog and the token say `collection`; CONTEXT.md
**Franchise** records the split). The prefixes are read from wherever NEU-1433 / NEU-1434 define
them (`news/subject_key.py` or `catalog_events.py`), never restated here. If either ticket has
not merged when this is implemented, its branch is written against the M2 contract's token
spelling and its test is marked to be enabled on merge — the builder must not wait on both.

### D-1437.5 — Story-backed cards: `first_association_clause`, the one builder M4 extends

EF-13. A resolved `story_person` mention (`path IN RESOLVED_MENTION_PATHS`) of a followed person
reaches that person's followers only as the first association or the first detachment. This is
**one builder**, called by `entity_attachment_event_ids` and, through it, by the notify pass; M4
adds a `story_entity` arm inside it, not beside it.

```python
def first_association_clause(*, user_id: UUID) -> Select[tuple[UUID]]:
    """`SELECT event.id` for the story-backed attach/detach cards whose resolved mentions make
    a followed entity's first association with, or first detachment from, the film (EF-13)."""
```

The predicate, for a published event `E` on film `F`, a story `S` attached to `E`
(`event_story`), and a mention `M` of followed person `P` on `S`:

**Attach arm** — all of:

1. `E.event_type IN (casting, crew_attached)` and `M.features->>'event_type' = 'casting'`.
   The card must be an attach card *and* the mention must be named in connection with the
   casting beat. Stories emit only `casting` for attachments (`link/cluster.py::_VALID_TYPES`
   has no `crew_attached`; a director attaching comes back as `casting`), so `casting` is the
   attach vocabulary on the mention side. Spell the set as a constant
   (`STORY_ATTACH_MENTION_TYPES`) so M4 and any prompt change have one place to widen.
2. **No earlier published attach card for `P` on `F`:** no published event on `F` of type
   `casting` / `crew_attached` with `created_at < E.created_at` that names `P` — by name token
   (D-1437.3's spelling) *or* by a resolved mention of `P` on one of its stories with an attach
   `event_type`. The two mechanisms are the same two the branch itself uses, so "a card names
   `P`" means one thing throughout the module.
3. **No current credit not attributable to `E`:** `NOT EXISTS film_credit(F, P)`, **or**
   `EXISTS film_credit_change(F, P, change = 'added', carded_by_event_id = E.id)`. The second
   disjunct is the carve-out decided in planning: a credit the catalog observed *after* the
   story, and that D-5 stamped as published by `E`, is `E`'s own confirmation, not a prior
   attachment. A baseline credit (no change row) still blocks: the person was on the film
   before anyone wrote about it, so the story is not the first association. A credit that
   arrived after `E` but was never stamped (outside `within_days`, or under a hold) blocks
   too — and in that case the sweep raises its own catalog card, which the name branch
   selects, so the follower still has a row.

**Detach arm** — all of:

1. `E.event_type = 'credit_removed'` and `M.features->>'event_type'` in a
   `STORY_DETACH_MENTION_TYPES` constant. **That constant is empty at M3**: the story vocabulary
   has no detach type, so no story-backed detach card exists yet and this arm selects nothing.
   It is spelled anyway, with a test that pins it empty, because it is the seam M4 (and a later
   prompt change) fills, and the ticket's contract is that both arms live in one builder.
2. No published `credit_removed` card for `P` on `F` created after the latest published attach
   card for `P` on `F` and before `E`.

The clause **does not** re-check the mention against the event's `subject_key`. A casting card
about performer X whose story also names director Nolan, with Nolan's mention typed `casting`,
is the case the "no current credit" term exists for: Nolan holds a credit on the film, so his
followers do not see X's card. If Nolan is *not* yet credited anywhere in our data, the card is
his first association with the film in our data, and it reaches his followers once. Accepted;
document it on the builder.

Mutes: the film-level exclusion is applied once, at the top of `entity_attachment_event_ids`,
not per branch.

### D-1437.6 — `canceled`

A `canceled` card (NEU-1435: `confirmed`, catalog, at most one per film, no `subject_key`)
reaches every follower of an entity **currently attached** to the film:

- person: `EXISTS film_credit(film, person)` for any followed person, any credit type, any
  billing, any job;
- company: `EXISTS film_production_company(film, company)` for any followed company;
- franchise: `film.collection_id IN` followed franchises.

Read at query time, on purpose: EF-6 chose `canceled` *because* TMDB rarely strips credits from a
cancelled film, so "currently attached" is the durable answer, and a follow created after the
cancellation still finds the card on the timeline (a director's cancelled film is part of their
stream). Title followers get the card through `title_follow_film_ids`; a user reaching it both
ways is one row (D-1437.2's distinct).

### D-1437.7 — The notify pass

`deliverable_events(since)` loses `Event.confidence == "confirmed"`. Everything else in it —
the `EventSummary` join, `Film.slug IS NOT NULL`, `visible_events()`, `region_visible()`,
`status = 'published'`, the `created_at > since` window — is unchanged, per
`notify-pass-visibility-rules`.

`digest_event_ids(session, *, user_id, since)` reads `deliverable_events(since)` ∩ the new
clause. `today` and `excluded_statuses` leave its signature, and `decide_for_user`,
`run_notify_pass` and `pipeline_run` stop threading them (`max_age_days` stays until NEU-1439
retires `watchlist_film_ids`; if the alert branch below no longer reads it, drop it here and
leave the setting for the digest slate).

`alert_event_ids` reads `deliverable_events(since)` ∩ the new clause ∩
`Event.confidence == "confirmed"` ∩ `Event.event_type IN PUSH_WHITELIST`, plus the Python-side
`now_available` store check as today. That is the **interim** push rule: the same films and
events as the digest, cut to the old whitelist and to confirmed cards. Consequences to state in
the PR body and pin with tests: an entity follower earns no push at all from this ticket
(nothing in `PUSH_WHITELIST` is an attach type), and `canceled` does not push yet. NEU-1438
replaces the whitelist and the confidence term with EF-7 / EF-8; it must not have to touch the
clause.

`test_a_rumored_event_is_never_queued_in_any_kind` becomes two tests: a rumored card on a
followed film is queued as `digest` and not as `alert`.

### D-1437.8 — Deletions and what stays

Deleted from `app/follow_queries.py`: `followed_film_ids`, `events_naming_followed_people`, and
the person-coverage helpers if NEU-1432 left any (`_coverage_credit_clause`,
`lead_credit_clause`, `LEAD_TOP_BILLED_ORDER`, `credit_tier` — NEU-1432 deletes them; verify).
`_company_film_ids` and `_person_covered_film_ids` stay while `covered_film_ids` does.

Kept, untouched, for NEU-1439: `covered_film_ids`, `covering_follows`, `watchlist_film_ids`,
`covered_by_any_user_clause`, `muted_film_ids`, `_not_muted`, and their callers
(`public/service.py` calendar and iCal, `digest_sender` slate, `watchlist_service`,
`ingest/videos.py` / provider poll). The PR body says so, and says mutes are honoured in the new
builders until that ticket.

The module docstring is rewritten: the "two questions, two sets" framing goes; the new framing
is "a title follow selects films, an entity follow selects events" with the branch list above
and the name-match caveat. The `Event.subject_key` comment in `news/models.py` that points at
`events_naming_followed_people` points at `first_association_clause` instead.

### D-1437.9 — Documents

- `CONTEXT.md` **Digest**: carries rumored cards as the timeline does (EF-7, EF-10); the push
  is what waits for confirmation. **Timeline** already states the clause; add the
  first-association sentence for story mentions. **Attach** gains: "a second outlet attaching
  to an existing card does not re-alert (EF-13)".
- `docs/specs/bl-entity-follows-project-spec.md` §6 M3 shared contracts: `first_association_clause`
  named beside the two builders; `deliverable_events()` loses the confidence floor and the
  alert branch carries it until NEU-1438.
- AGENTS.md `notify-pass-visibility-rules` (or wherever it lives): confidence is no longer one
  of the shared selector's terms.

### What does not change

- `get_feed_grouped`, `_feed_scope`, the feed DTO, day grouping, pagination.
- `visible_events()`, `region_visible()`, the summary join, the slug term.
- The sweep, the cluster stage, `credit_confirm.py`, `subject_key` writers.
- `PUSH_WHITELIST`, `ALWAYS_ON_ALERT_TYPES`, the `now_available` store check (NEU-1438).
- The calendar, iCal feed, digest slate, provider poll set, `/me/watchlist` (NEU-1439).
- `followed_people()`, the recorded grade, the sweep tranche (NEU-1432).

---

## Acceptance criteria

### `app/follow_queries.py` (`tests/integration/app/test_follow_queries.py`)

- `title_follow_film_ids`: a title follow reaches its film in any status and at any age; a
  person, company or franchise follow puts **no** film in it; a muted title-followed film is
  absent; a non-UUID `entity_id` is skipped, not raised.
- `entity_attachment_event_ids`, people: a followed person's published `casting`,
  `crew_attached` and `credit_removed` cards are selected by name token; a `release_date`,
  `trailer` or `announced` card on the same film is not; a superseded attach card is not; a
  card on a muted film is not; a person whose name normalizes differently (accent, double
  space, case) still matches; the `ß` divergence is pinned as a known miss.
- companies / franchises: `company:<id>` and `collection:<id>` tokens select the four EF-5
  types for their followers and nothing else; a title follow on the same film reaches those
  cards through the film term, not this one.
- `canceled`: reaches a person follower at any credit (a 40th-billed cast credit, a third-unit
  crew job), a company follower, a franchise follower; not a person follower whose credit was
  removed; a title follower through the film term.
- The union is distinct: a user following the film's company and its director sees one row for
  the `canceled` card.
- `only=` narrows a branch to one follow.
- `sql_normalized_name` ≡ `normalize_name` over the fixture list (parity test, unit or
  integration).

### `first_association_clause`

- A story-formed `casting` card whose resolved mention names a followed person with no prior
  attach card and no credit → selected. Same person, same film, a second story attached to the
  same card → still one event id. A second story that formed a *second* casting card (split
  beat) → the later card is not selected.
- The person already holds a baseline credit on the film (no change row) → not selected.
- D-5 case: story card first, then the catalog credit arrives and is stamped
  `carded_by_event_id = card` → still selected. Same, but the change row is unstamped → not
  selected by this clause (and, when the sweep cards it, the name branch selects that card).
- Mention `path = unlinked` or `not_in_tmdb` → never selected. Mention with `event_type` null
  or `other` (an interview) on an attach card → not selected. Mention on a `release_date` card
  → not selected.
- The detach arm selects nothing while `STORY_DETACH_MENTION_TYPES` is empty (pinned).
- A test asserts `entity_attachment_event_ids` and `digest_event_ids` / `alert_event_ids`
  reach the clause through the one builder (e.g. monkeypatch `first_association_clause` and
  observe the call from each).

### `public/service.py::get_timeline` (`tests/integration/routers/test_timeline.py`)

- The ticket's five: a person follower sees the attach card and not the film's trailer; a title
  follower sees both; a second outlet attaching to the existing event adds no row; an interview
  mention adds nothing; `canceled` reaches a company follower.
- A person follow no longer reaches the film's other beats, released or not
  (`test_following_a_director_shows_only_their_films`,
  `test_every_seed_grade_credit_counts_and_nothing_below_it_does`,
  `test_a_person_follow_does_not_reach_films_out_of_play`, the company and franchise
  film-reach tests are rewritten to the new rule, not deleted).
- A timeline row reached by one event carries that event only (the existing
  `event_count` / `event_types` behaviour for event-grained matches).

### `app/services/notify_service.py` (`tests/integration/app/test_notify_pass.py`)

- A rumored attach card on a title-followed film is queued as `digest`, not `alert`.
- A catalog `casting` card (rumored) for a followed person is queued as `digest` for the
  person's follower; no `alert` row; no row for a non-follower.
- A title follow on a whitelist beat still queues `alert` (mail and push) and `digest`.
- An entity follower earns no `alert` from any card (interim, pinned so NEU-1438 flips it).
- `test_the_digest_covers_events_that_merely_name_a_followed_person` becomes its negative: a
  `release_date` card whose story names a followed person is **not** in their digest.
- The remaining visibility tests (hidden type, no summary, region, slug, watermark, re-run,
  suppression) pass unchanged.

### Tooling

- `task format`, then `task test && task lint && task typecheck` inside the container.
- PR body: which builders stay for NEU-1439 and why; mutes honoured; the interim push rule and
  the two pushes it does not send (entity attachments, `canceled`); the `ß` / renamed-person
  caveat.

---

## Out of scope / deferred

- The per-type push whitelist, seed-grade clause and provenance rule (NEU-1438).
- Deleting the watchlist builders, endpoints, mutes and the film readers (NEU-1439).
- `last_activity_at` and the entity `/events` endpoints (NEU-1440); the `only=` seam is left
  for them.
- The `story_entity` arm of `first_association_clause` (NEU-1446).
- Stamping sweep-carded change rows or adding id tokens to `subject_key`; if name matching
  proves lossy in production, that is the follow-up, and D-1437.3 is where it lands.
- A story-side detach vocabulary; the arm is spelled and empty.
- A detach card for a baseline credit that leaves (the sweep's removal gate wants a prior
  visible attach card; NEU-1436 narrows the gap going forward).
