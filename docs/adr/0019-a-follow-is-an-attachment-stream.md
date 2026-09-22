# A follow on a person, studio or franchise is that entity's attachment stream; only a title follow covers a film

**Status:** accepted (2026-09-20, bl: Entity Follows planning session)
**Supersedes:** ADR-0018 in part (decisions 2 and 4, and the alert-window amendment); project
spec D-11 (film-coverage half), D-42 to D-50 in part, D-45 wholly
**Relates to:** `docs/specs/bl-entity-follows-project-spec.md` (EF-1 to EF-22); ADR-0002,
ADR-0014 (baseline rule, now with one exception), ADR-0017

## Context

ADR-0018 made every follow *cover* films. A person follow put that person's films on a
computed watchlist at a chosen coverage tier (`lead`, `major`, `any`), and every beat on those
films — dates, trailers, availability — alerted the follower as if they had followed each film
themselves. Company and franchise follows covered every matching film outright. The watchlist
was the computed set, a **mute** took a film out of it, and the film page's one button spoke
of following "via Christopher Nolan".

Tom reopened this on 2026-09-20, the day the tiers shipped. The objection was to the premise:
following a director is not a request to be told about every trailer of every film they
direct. It is a request to be told *when they take a film on, or leave one*. Everything else
about a film is the film's own news, and the user can follow the film. Under that reading the
tiers were solving a volume problem the model had created — the firehose only exists because
a person follow fans out to whole filmographies — and the watchlist, the mute and the "via"
line were all machinery for managing that fan-out.

ADR-0018 had considered and rejected "only title follows alert" because it hard-coded an
asymmetry between entity types. This decision keeps the asymmetry and makes it the product:
a film is a thing with a slate of beats; a person, studio or franchise is a thing that joins
and leaves films.

## Decision

1. **A follow is binary** (EF-1). No coverage, no tiers. A person follow reaches every credit
   at any billing or crew job (EF-2), because "which of their credits count" was a coverage
   question and there is no coverage.
2. **An entity follow delivers the entity's attachment stream and nothing else** (EF-3): the
   cards in which the entity joins or leaves a film, and the film's cancellation. On the
   timeline, in the digest, on push and email. No other beat on those films reaches the user
   through that follow.
3. **A title follow delivers everything** about the film on the timeline and in the digest,
   and pushes on D-32's beats, on cancellation, and on seed-grade cast and crew joining or
   leaving (EF-7, EF-9).
4. **Admission is an attachment for a followed entity** (EF-4). ADR-0014's "first
   observation is a baseline, never a change" gains its one exception: a credit, company row
   or collection held by an entity somebody follows *at the moment the film is first
   observed* is recorded as `added`. Without it the most common attachment — a director's next
   film entering the catalog with the director already on it — would be silent.
5. **Studios and franchises are observed and resolved like people** (EF-5, EF-12): change
   tracking, attach and detach event types, quarantine, supersession, and story resolution of
   their own. One rule for all three types; a different rule per type is the drift the glossary
   exists to stop.
6. **A story mention counts once**, as the first association or the first detachment of the
   entity with the film (EF-13). Rumored associations wait for confirmation before pushing
   (EF-10); a confirmed trade story pushes immediately (EF-11); a catalog attachment that has
   cleared quarantine is confirmed for the push decision by construction (EF-8).
7. **The watchlist and the mute are gone** (EF-14). With nothing indirect covering films, the
   computed set is exactly "title follows", a mute is an unfollow, and the two pages ADR-0018
   kept collapse into one: the follows page, with Films as a filter (EF-15). The calendar, the
   iCal feed, the digest slate and the provider poll set read title follows.
8. **The film page has one button and every name is a link** (EF-16); an entity's page is
   the only place its follow starts, and it shows the entity's own cards (EF-17, EF-18).

## Considered alternatives

- **Keep coverage, narrow the defaults.** Make `lead` narrower, or default `any` off. Rejected:
  the tiers manage fan-out that the model should not have; a narrower firehose is still a
  firehose.
- **Attachment stream plus the film's beats for the entity's films** (today's D-11 timeline
  with only alerts narrowed). Rejected by Tom in the interview: "if the user wants all updates
  on the movie, they have to follow the movie itself" is the rule, and a timeline that
  quietly kept the fan-out would contradict the alerts.
- **People only; companies and franchises stay on every-film coverage.** Rejected: one rule
  per type is the ADR-0018 drift in reverse, and the observation gap for companies and
  collections was a missing feature, not a reason to keep the old model.
- **Push rumors to entity followers** ("for a person follower the rumor is the news").
  Rejected: it pushes twice per attachment and retracts some; D-32's "nothing unconfirmed
  pushes" stands, with quarantine as the catalog's confirmation.
- **Cancellation as detachments.** Rejected: TMDB rarely strips credits from a cancelled film,
  so the detach cards would not come, and when they did they would misdescribe what happened.

## Consequences

- Coverage tiers (D-43, D-47, D-48), the alert window as an entity-follow concept (D-46), the
  computed watchlist (D-42, D-45), `app.watchlist_dismissal`, the `/me/watchlist` endpoints
  and page, the Muted section, the "via …" line, the seed-row follow buttons on the film page
  and `lib/follow-labels.ts` are all removed. NEU-1414, NEU-1415, NEU-1405, NEU-1417,
  NEU-1418 and NEU-1419 are superseded in the parts named; their specs stay as history.
- The recorded grade and the followed sweep tranche (D-49, D-50) survive, widened to every
  followed person; `SWEEP_ADMIT_FOLLOWED` goes on.
- New tables `catalog.film_company_change`, `news.story_entity`, `app.import_candidate`; new
  column `film.companies_observed_at`; new event types `company_attached`,
  `company_removed`, `collection_attached`, `collection_removed`, `canceled`.
- The notify pass decides per follow type (EF-7) and per provenance for attach and detach
  cards (EF-8); `deliverable_events()`'s visibility terms are unchanged. Its `confidence =
  'confirmed'` term is **not** a visibility term and does move: NEU-1437 took it out of the
  shared selector and into the alert branch, because every catalog attachment is `rumored`
  until its quarantine clears and EF-7 gives the digest everything the timeline carries.
- The importers write title follows only, after review (EF-20 to EF-22).
- The glossary entries **Coverage**, **Watchlist** and **Mute** are retired; **Follow**,
  **Timeline**, **Push whitelist**, **Recorded grade**, **Seed person** and **Tranche** are
  rewritten; **Attachment**, **Studio** and **Last activity** are added.
