# A follow is the only thing a user maintains; the watchlist is computed from follows and mutes

**Status:** accepted (2026-09-20, NEU-1405 planning session)
**Relates to:** project spec D-10 to D-14 (superseded in part by D-42 to D-45), D-31 to D-34,
D-37 to D-41; milestone M8; NEU-1413, NEU-1414, NEU-1405, NEU-1415

## Context

The consumer-pivot spec split a user's interest into two records. A **follow** on a person,
company, franchise or title produced timeline rows and nothing else (INV-7). A **watchlist
item**, per film, was "the only thing in the system that produces a push", carried per-item
store preferences (`alert_prefs`, D-14), and was either added by hand or **derived** from a
follow when the film was in play and the followed person was its director or in its top-3
billing, or the followed company, franchise or title matched (D-13). Removing a derived item
wrote a permanent **dismissal**.

By 2026-09-20 every path but one already treated a title follow as implying a watchlist item:
D-13 derived one, and both importers wrote both rows per film. The one exception was the film
page's own Watchlist button, which wrote only the item. Because the timeline reads follows
only, a film added that way never reached the timeline. On the film page the two controls
came apart in ways nobody had designed: unfollowing the title left the derived item; removing
the item left the follow and wrote a dismissal that would block re-following the title from
ever re-adding it. The `/me/watchlist` and `/me/follows` pages each carried a sentence
explaining the difference; the film page, where the choice is made, carried none. That gap was
filed as NEU-1405.

The glossary's single stated reason for two records was volume: following a prolific actor
must never become a push firehose. That is an argument about *which of a person's films*
should alert, which D-13 answered with its director-or-top-3 cut. It is not an argument for
the user maintaining a second list.

## Decision

1. **One user-facing concept: follow** (D-42). Every follow feeds the timeline *and* alerts.
   INV-7 is withdrawn.
2. **Coverage lives on the follow** (D-43). A person follow carries `coverage ∈ lead|all`,
   default `lead`: `lead` alerts on films where the person is director or top-3 billed (the
   D-13 cut, kept as the default precisely because it is what stops the firehose); `all`
   alerts on every seed-grade credit (the D-11 cut). Company, franchise and title follows
   cover every matching film. Timeline coverage stays D-11 for every follow; coverage narrows
   alerts only.
3. **One store setting per user** (D-44). `user_settings.alert_stores` (subset of
   `buy|rent|stream`, default `{stream}`) replaces per-item `alert_prefs`. There are no
   per-film preferences. The always-on push whitelist beats (D-32) are unchanged.
4. **The watchlist is computed, and removal is a mute** (D-45). A film is on the user's
   watchlist when it is in play and at least one follow covers it at its coverage. A **mute**
   removes it from alerts, the calendar, the iCal feed and the digest slate, but never from the
   timeline, and is reversible. The existing `watchlist_dismissal` table holds mutes; only the
   vocabulary changes. `POST /me/watchlist {film_id}` means *want* (clear any mute, create a
   manual title follow if nothing else covers the film); `DELETE /me/watchlist/{film_id}`
   means *stop* (delete a direct title follow, mute if still covered). The word "watchlist"
   survives as the name of the computed set; the film page shows a single follow control.

Whether the set is materialised into `watchlist_item` by the existing derivation pass or
queried on demand is left to NEU-1414. The API contract on milestone M8 is what the frontend
builds against either way.

## Considered alternatives

- **Keep both records and add the cue** (the ticket as filed). Cheapest, and it leaves the
  incoherent pairs on the film page in place with a sentence over them. Rejected: the
  explanation was hard to write because the split was not real.
- **Only title follows alert.** Person, company and franchise follows stay timeline-only and
  derive title follows, so the watchlist is "the titles you follow". Coherent and closer to
  today's data, but it keeps the firehose rule as a hard-coded asymmetry between entity types
  rather than something the user can widen. Rejected in favour of coverage on the follow.
- **Hoist per-item prefs to the follow.** Each follow carries its own store subset and the
  per-film override survives. Rejected: no one has asked to hear about buy availability for
  one director and stream availability for another, and the per-film record it preserves is
  the thing being removed.

## Consequences

- The film page has one control under the title, keyed on the watchlist item, with a static
  cue beneath it (NEU-1405). `/me/watchlist` loses its chips and its permanent-removal
  confirm, gains a "via …" line per row and a Muted section; `/me/follows` gains the coverage
  control; `/settings` gains the store set (NEU-1415).
- Importers write a title follow per film and nothing else. A one-shot migration turns manual
  watchlist items into manual title follows and keeps dismissals as mutes (NEU-1414).
- A hand-followed film now appears in the timeline, which it never did as a watchlist item.
- D-40 is unchanged: follows, mutes and settings survive an entitlement lapse. D-39 gates every
  route involved. The M7 delivery passes read the same computed set as the calendar and iCal
  feed, so a mute is honoured everywhere at once.
- The glossary entries **Follow**, **Watchlist**, **Coverage** and **Mute** in `CONTEXT.md`
  replace **Watchlist item** and **Derived watchlist item**.
