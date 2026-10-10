# The site tracks a film to its first home availability, and no further

**Status:** accepted 2026-10-08 — implementation tracked in NEU-1542 (bl: Maintenance).
Supersedes D-29 of *bl: Consumer Pivot* (the where-to-watch box). Amends D-26 (the home
release is digital alone; physical is dropped), D-34 (the iCal feed carries theatrical and
digital) and DC-9 of *bl: Digest Content* (three displayable buckets). D-27 and D-28 (the
provider poll and `now_available`) stand. FB-26 (the slate is the my-films calendar) stands.
**Relates to:** ADR-0014 (catalog-sourced events), ADR-0021 (the digest is the only delivery),
`docs/specs/NEU-1542-calendar-split-and-initial-release-only.md`.

## Context

The consumer pivot gave the product a home-release half (D-26 to D-29): US digital and
physical release dates became displayable and calendar-visible, a daily provider poll began
recording the first time a film was seen on any service, and the film page grew a
**where-to-watch box** listing the film's current US carriers by how a reader pays. The box
read a snapshot the poll rebuilt wholesale every run.

Three things about that half turned out to cut against what the site is for.

1. **The physical date is noise.** Few readers wait for the disc, and TMDB's type-5 date
   routinely lands after the film is already streaming, so on the calendar, the slate and the
   feed it was a late, low-value beat sitting beside the beats that mattered.
2. **The box promised an accuracy over time the site does not have.** The poll stops reading
   a film 365 days after release; films released before the site launched were never polled;
   and even where the snapshot was correct, "where can I watch this today" is a question about
   a *released* film, which is the one kind of film this site is explicitly not about. A box
   that is right for a year and then quietly stale — or absent for every older film — invites
   the reader to rely on the site for something it will not do.
3. **The calendar mixed the two audiences.** A reader planning a cinema trip and a reader
   waiting for a title to come home are looking for different dates, and one list under one
   date heading served neither.

Tom's framing (NEU-1542): the site provides information about *upcoming* movies. A released
film ceases to be updated beyond the window from theatrical to its first home availability.

## Decision

1. **The release window ends at first home availability.** The site's home-release facts are
   exactly two: the **US digital release date** TMDB announces (type 4, displayable, carded as
   a `release_date` event when set or moved — D-26's surviving half), and the first-observation
   **`now_available`** event per (film, monetization type) — D-28, unchanged. Nothing records or
   shows a film's *current* carriers: `catalog.film_availability_current` and the where-to-watch
   box are removed, and the provider poll writes the insert-only ledger alone. The film page
   points at TMDB's own watch page once a US date has passed; it hosts no provider data.
2. **Physical (TMDB type 5) is not a displayable release.** `HOME_RELEASE_TYPES` is `{4}`,
   `RELEASE_TYPE_BUCKETS` has three entries, and every surface that derives from them — the
   film page's release list, both calendars, the `.ics` feed, the slate and its markers, the
   release-date carder and its summaries — drops the bucket in one edit. Storage stays
   unfiltered (the load-bearing property `catalog/release_grade.py` documents), so the change
   backfills nothing. Cards already raised with a `US:physical` token are repaired by a one-off
   script: physical-only cards deleted, mixed cards re-rendered without the physical line.
3. **The calendar has two kinds, by where you watch.** "In theaters" (the US theatrical arc)
   and "At home" (the US digital date), chosen by an in-page control that is a view, not a
   place: never a URL, never a remembered preference, shared across the My films / All releases
   tab. The `.ics` feed and the digest slate stay single and carry the union, because a
   subscribed calendar and a 30-day mail list are not where the two audiences diverge.

## Considered alternatives

- **Keep the snapshot table but hide the box.** Rejected: an unread table maintained by a daily
  delete-and-rebuild is a cost with no reader, and "for later" is the reasoning this ADR exists
  to refuse — a later surface for current carriers would be the same promise under a new name.
- **Hide physical at read time** (a type check in `region_visible`, the renderers and the
  calendar). Rejected: every reader grows a rule, the stored bodies still say "physical", and
  the glossary would carry a bucket the product does not show.
- **List observed availability on the home calendar.** Rejected: the calendar is upcoming-only
  and lists announced dates; a past observation on it breaks that rule and the slate-equals-
  calendar contract (FB-26), and the digest and `.ics` would have had to follow.
- **Two routes for the two kinds.** Rejected for D-1412.1's reasons: `/calendar` stays one
  indexable address and the view is state.
- **A JustWatch link.** Rejected: TMDB's API carries no JustWatch URL, only a link to TMDB's
  watch page, and a search URL has no stable identity.
- **Collapse rent and buy into one beat.** Rejected for now (D-1542.9): seen together they
  already share one card; seen apart the second card is rare and honest.

## Consequences

- `now_available` cards keep naming streaming services, so **JustWatch attribution stays**
  wherever they render (DC-17, NR-8). That is TMDB's condition on the data, and the data still
  renders; what left is the box, not the beat.
- `GET /calendar` and `GET /me/calendar` take `kind=theatrical | home`; omitted means both, so
  deploy order between the two repos is free.
- A subscriber's physical `.ics` events vanish on the next fetch (clients drop what a feed
  stops publishing). Theatrical and digital UIDs are unchanged, so nothing duplicates.
- The provider poll's set, cadence and ledger are untouched; `watch_provider` stays for the
  bodies and the re-render script.
- **Amended by NEU-1538 (2026-10-08):** an observed offer counts as first home availability
  only from the film's US digital date. TMDB passes pre-orders on as plain `buy` offers, so the
  poll holds a film's offers — no ledger row, no card — until its US type-4 governing date is on
  or before the observation day (D-1538.1–2). The first-observation rule itself is unchanged;
  what changed is when an observation is believed.
- Anything that later wants "where is this film available *now*" must reopen this ADR, not
  add a reader to a ledger that was never meant to answer it.
