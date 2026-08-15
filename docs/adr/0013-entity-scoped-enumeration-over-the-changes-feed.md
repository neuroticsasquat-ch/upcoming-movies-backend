# Discover undated films by walking tracked people's credits, not the TMDB changes feed

**Status:** accepted

## Context

`/discover/movie` enumerates by `primary_release_date`, and a date-range filter cannot match a
film with no date. Undated, just-announced projects are therefore structurally unreachable — the
gap the prerelease ingestion spec named **Path B** and documented as out of scope. The spike
NEU-285 carried two candidate approaches forward: polling TMDB's `/movie/changes` feed, or
following people and production companies we already track.

Two facts, confirmed against the API surface and the current catalog, decide it.

**The changes feed has no pre-filter.** `/movie/changes` returns `{id, adult}` per record and
nothing else. There is no title, no status, no popularity — so every changed id costs a full
`/movie/{id}` details fetch *before* it can be judged. At the configured 40 req/10 s that is
hours per day for a payload that is overwhelmingly back-catalog metadata edits. The cost is
proportional to *TMDB's* churn, which we neither control nor care about.

**Person credits are one request for a whole filmography.** `/person/{id}/movie_credits` returns
every film a person is attached to, undated entries included, in a single call. Cost is
proportional to *our own* catalog. Better still, the entities we already track *are* the relevance
prior: a film a director we track has just been attached to is, by construction, a film our users
would care about. `catalog.film_credit` is already populated, so no new upstream data is needed.

Production companies were evaluated on the same terms and fail them. There is no undated-only
discover filter, so company enumeration means paging a company's entire filmography with the date
bounds dropped — sorted by popularity, while the undated films we want sit at the bottom. For an
indie that is one page; for Universal it is 100+ pages of released back catalog. The cost
concentrates on the majors, whose slate entries are also the least informative.

## Decision

Discover undated films by **enumerating the credits of seed people** — anyone holding a
director, writer (`Writer`/`Screenplay`), or top-5-billed-cast credit on an active, non-dormant
film. 7,519 distinct people today, ~31 minutes per sweep.

The changes feed is **rejected, not deferred**. Companies are excluded on the paging economics
above; their projects are reached through their directors and stars anyway.

## Considered alternatives

- **`/movie/changes` polling.** Rejected on the pre-filter argument: unbounded cost we do not
  control, spent almost entirely on records irrelevant to an upcoming-film tracker.
- **A hybrid — changes feed narrowed by some cheap signal.** Rejected: there is no cheap signal
  to narrow it *by*. The payload carries only an id and an adult flag.
- **Company-seeded discover.** Rejected — see above.
- **Producers as a seed grade.** Rejected: 2,191 additional people whose attachment is the
  weakest signal of the available roles. An EP credit travels far and says little about whether
  a project is real.
- **A popularity floor on undated candidates.** Not viable: the catalog's p10 popularity is 1.22,
  sitting on the existing 1.0 discover floor, and undated films are far below it by definition.

## Consequences

- **Discovery is reflexive.** We can only find films adjacent to what we already track; a debut
  director's first project at an unknown company is invisible. Accepted as a v1 boundary, and
  better than ingesting everything TMDB touched and hoping a relevance bar holds.
- **The seed set is self-expanding** — admitted films contribute their own credits as seeds.
  Bounded by dormancy (ADR-0015), which removes dead films' credits from the seed query.
- Sweep runtime is linear in the seed count, so the seed set's size is an operational number to
  watch, not a static one.
