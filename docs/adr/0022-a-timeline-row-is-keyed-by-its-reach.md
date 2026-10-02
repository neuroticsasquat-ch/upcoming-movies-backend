# A timeline row is keyed by the follow that reached it, and the digest reproduces the timeline

**Status:** accepted (2026-10-02, bl: Feed by Followed Entity planning session)
**Amends:** ADR-0019 (the timeline is still selected by EF-3's where-clause, but a row is no
longer "a feed row"); ADR-0021 stands (the digest is still the only delivery).
**Relates to:** `docs/specs/bl-feed-by-followed-entity-project-spec.md` (FB-1 to FB-27),
ADR-0016 (publication-day grouping, unchanged), NEU-1199 (one row per film, day and section).

## Context

The timeline has been "the feed with a where-clause": `GET /me/timeline` answered with
`/feed/grouped`'s exact row shape, one row per (film, day, section), selected by `film IN
(titles you follow) OR event IN (attachments of entities you follow)`. That made one DTO serve
both surfaces, but it threw away the one fact the feed never has and the timeline always does:
*which follow put this here*. A director's attachment to a film the reader has never heard of
rendered as that film's row, headlined by a title that meant nothing to them, with the
director's name buried in a summary line. The digest had the same shape and patched the gap
with a "Following: Denis Villeneuve" line under the film header.

Tom's objection (2026-10-02): a reader who follows a person wants that person's news headed
by the person. The film is what the line is about; the entity is why it is there.

## Decision

1. **A timeline row has exactly one reach** — a title follow, or one person, studio or
   franchise follow — carried as a nullable `via` on the row. A row is keyed (reach, film,
   day, section). A card reached three ways is three rows. The global feed's rows have no
   reach and `via` is always null there; the DTO stays one type.
2. **A timeline day is laid out in follow blocks** — Films, People, Studios, Franchises — each
   split into In the news and Not yet reported exactly as a feed day is. The Films block is
   today's layout over title follows only. The other three hold one entity row per followed
   entity, headlined by the entity, each line naming the film before the summary.
3. **The digest is the timeline reproduced in mail.** The daily renders the timeline day,
   headings, poster strip and all; the weekly renders the same blocks and sections with one
   entry per film or entity across the week; the slate renders the my-films calendar for its
   dates. The lead card, the entry cap and the "Following:" line are retired. The subject rule
   survives because it is computed, not laid out.

## Considered alternatives

- **One row, a list of reaches.** Keep (film, day, section) rows and add `via: [...]`. Rejected:
  the Films block would then have to be derived on the client from a title entry in the list,
  the entity blocks would need the row split anyway, and the digest would re-derive the same
  split in Python. One row per reach is the shape every consumer wants; de-duplication is the
  thing that was wrong.
- **Two lists, or a second endpoint**, for entity rows. Rejected: day pagination would have to
  span two sources, and the timeline would stop sharing the feed's grouping helpers.
- **Keep the digest film-grouped and add block headings.** Rejected by Tom: the mail would
  headline the film, which is the thing the timeline just stopped doing.

## Consequences

- `GET /me/timeline` changes meaning without changing shape: a film-day reached by a title
  follow and by a director follow is now two rows, and an entity-reached film-day carries
  only the events that reached it. **An old frontend given the new rows renders one film twice
  under a day and collides on its React keys**, so the frontend deploys first and tolerates a
  missing `via` (FB-9).
- The digest's DC-3 (film-grouped daily), DC-4 (status line), DC-6 (Following line) and DC-8
  (cap) are superseded. DC-5's line form survives under In the news; DC-7's subject survives.
- `follow_attribution_pairs` becomes the one builder both the timeline's entity rows and the
  mail read, which is what keeps the page and the mail from disagreeing about who reached what.
- The glossary gains **Reach**, **Follow block** and **Entity row / entity entry**; **Timeline**,
  **Digest**, **Slate**, **Lead film** and **Film entry** are rewritten.
