# The product is US-market, and says so

**Status:** accepted 2026-10-09 — implementation tracked in NEU-1545 (bl: Maintenance).
Generalises D-26 of *bl: Consumer Pivot* (the home release is US-only) from the digital half
to the product. Restates, without changing, decision 1 of NEU-1397 (the headline release and
the film page keep origin-country theatrical dates). **Relates to:** ADR-0023 (the release
window ends at first home availability), `docs/specs/NEU-1545-us-market-explicit.md`.

## Context

TMDB's `/movie/{id}/release_dates` returns every country and every type, and
`rebuild_release_dates` stores all of it unfiltered. Every narrowing is ours:
`catalog/release_grade.py` admits the theatrical arc for US plus the film's origin countries
and the digital date for US alone (D-26); the calendar, the iCal feed and the digest slate read
US rows only; the provider poll reads the US region; the film page's watch link opens TMDB's US
locale; the certification prefers the MPAA rating; dates and currency format as `en-US`. Non-US
digital dates exist in TMDB — thinner and noisier than theatrical, but present for GB, DE, FR and
others — and the site drops them on purpose.

None of this was said to the reader. The calendar page's heading was "Calendar", its kinds "In
theaters" and "At home"; the iCal feed was named "backlotter — your films" with summaries "in
theaters" and "digital"; the settings copy and the digest slate named no country. The only "US"
a reader could find was the calendar's SEO meta description. A reader outside the US had no way
to learn, short of noticing which dates were missing, that the site was not describing their
market.

The question raised in NEU-1545 was whether to widen instead: a country selector, or a North
American scope. Neither survives contact with the data or the pipeline. TMDB's digital coverage
outside the US cannot back a selector's promise, and every stage downstream of the cut is keyed
on one region — the `US:digital` subject tokens, `region_visible`, the `now_available` ledger,
the slate, the digest. "North America" would imply Canadian and Mexican dates and carriers;
TMDB keys CA and MX separately and the site reads neither.

## Decision

1. **backlotter is a US-market product.** Release dates, availability, ratings, formatting and
   outbound links are for the United States. The region is one value, the **primary region**
   (`release_grade.PRIMARY_REGION`), and the code spells it once: `public.service`'s
   `CALENDAR_REGION` is removed in favour of it.
2. **The film page's origin-country theatrical rows are the one deliberate exception.** A
   film's opening in its own market is part of the film's record, not a date the reader can
   act on, so the film page lists it (tagged with its country) and the headline release may
   lead with it (NEU-1397 decision 1). The calendar, the iCal feed and the slate do not carry
   it: they are US calendars. This asymmetry is kept and stated, not closed.
3. **Every surface that lists dates without a per-row country says "US" once.** The calendar
   page (one subtitle under the heading, shared by both tabs and kinds), the iCal feed (an
   `X-WR-CALDESC`; the name and the event summaries are unchanged so nothing moves for current
   subscribers), the settings calendar section, the digest slate's intro, the film page's
   release list (a caption that also names the exception), and the global footer. The exact
   copy is in the NEU-1545 spec.

## Considered alternatives

- **A country selector.** Rejected: TMDB's non-US digital coverage cannot honour it, the
  pipeline would need a region on every subject token, visibility term, ledger row and digest
  line, and a selector that works for theatrical and not for digital is a broken promise on the
  half of the product that matters most.
- **North America.** Rejected: CA and MX are separate TMDB regions whose dates and carriers the
  site does not read. The label would claim coverage the data does not have.
- **Close the asymmetry by admitting origin-country theatrical dates to the calendar.**
  Rejected: the all-releases calendar would fill with openings a US reader cannot attend, the
  iCal UID `(film, bucket)` collides when a film has both a US and an origin limited date, and
  the slate would follow. If ever wanted, it is a separate ticket that reopens this ADR.
- **Close it the other way, making the headline release US-only.** Rejected: a film with only
  a GB date would read as unconfirmed on the follows page while the film page shows a real
  date, and NEU-1397 chose the displayable set for exactly that reason.
- **Say it in one place** (an About page or the footer alone). Rejected: the reader meets the
  calendar, the feed and the mail without passing an About page, and the subscribed feed has
  no footer.
- **Rename the iCal feed or every event summary.** Rejected: most clients take the name once at
  subscription time, so a rename never reaches current subscribers; a per-summary "US" repeats
  on every notification and rewrites every existing event.

## Consequences

- `PRIMARY_REGION` is the only region constant. Anything that wants a second region — a
  selector, a per-user market, a second calendar — reopens this ADR rather than adding a
  constant beside it.
- The glossary gains **primary region** (`CONTEXT.md`), and the frontend glossary carries the
  same term. Origin-country rows are described there as the exception, so copy that says "US"
  without the exception is wrong on the film page and right everywhere else.
- Copy is the deliverable. Widening any date surface beyond the primary region later means
  changing the copy this ADR introduced, which is the point: the words and the cut move
  together.
