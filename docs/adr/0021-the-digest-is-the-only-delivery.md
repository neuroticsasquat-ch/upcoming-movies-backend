# The digest is the only delivery

**Status:** accepted 2026-09-26 — implementation tracked in NEU-1470 (bl: Maintenance).
Supersedes D-32, D-36 and D-44 of *bl: Consumer Pivot*, EF-7 to EF-11 of *bl: Entity
Follows*, and the push clauses of ADR-0019 (decisions 2 and 3).

## Context

A beat on a followed film could reach a user twice: as an **alert** — one mail the night it
was carded, for the beats on a per-reach push whitelist (D-32, EF-7), with a Web Push twin for
anyone who had enabled it (D-36) — and again the next morning as a line in their **digest**,
which carries every card the timeline carries (EF-7). The two were designed as "different
deliveries of the same news, not duplicates of one" (`app.notification`'s docstring). A
reader saw one trailer and two mails about it, an hour apart, and asked why.

The whitelist earned its keep only if an interrupt was worth more than a next-morning line.
Nothing showed that it was: the alert said less than the digest (no parenthetical, no
attribution, no date), Web Push had never been exercised against a real push service, and the
store-availability setting that existed to narrow one whitelisted beat (D-44) was the only
notification preference in the product.

## Decision

- **One delivery.** A follow's news arrives in the digest and nowhere else. The daily and
  weekly cadences, the slate day and the `off` choice are as built (D-33, DC-1, DC-2). How soon
  a reader hears about a beat is the cadence they chose; there is no faster channel.
- **No interrupt channel.** The alert mail, its send pass, the Web Push channel, the
  `push_subscription` table, the VAPID keys, the service worker and the settings toggle are
  removed. `app.notification` keeps only `kind = digest`, `channel = email`; the alert and push
  rows already written are deleted rather than kept as a dead vocabulary.
- **No per-beat preference.** `user_settings.alert_stores` goes with the beat it narrowed.
  The digest stays exactly "everything the timeline carries" (EF-7's digest half, DC-15): a
  reader who wants less news unfollows.
- **Confirmation no longer gates delivery.** EF-8, EF-10 and EF-11 decided *when a push
  fires*; with no push there is nothing left for them to decide. A `rumored` card is a digest
  line marked Unconfirmed (DC-5), and its later confirmation is a state change on the card,
  not a second delivery.

## Considered options

- **Keep push as the only interrupt.** Rejected: it keeps the whole whitelist, the per-reach
  decision and the store setting alive for a channel no one has seen work, and it makes
  "which beats interrupt" a question the product still has to answer.
- **Make the alert mail opt-in.** Rejected: an opt-in still needs the whitelist, the second
  sender and the second template, and the mail it sends is the worse of the two.
- **Mark former alert beats in the digest.** Rejected: DC-3's ranking already puts a date or a
  trailer above a credit beat, and a pill would keep the whitelist as a presentation rule.

## Consequences

- The `notify` slot becomes the decision pass alone: it queues digest rows and sends nothing.
  Its deadman check stays.
- The **alert window** keeps its name (the poll, the entity page and the import key on it) but
  no longer bounds any alert. A rename is not worth the churn.
- Reversal costs a rebuild: the push subscriptions are gone, and a browser must re-register.
