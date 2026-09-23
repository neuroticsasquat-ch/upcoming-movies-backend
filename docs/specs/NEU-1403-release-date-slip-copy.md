# NEU-1403 — Release-date copy flags a slip when the new date is later

**Ticket:** [NEU-1403](https://linear.app/neuroticsasquatch/issue/NEU-1403/release-date-copy-flags-a-slip-when-the-new-date-is-later)
**Parent story:** NEU-1335 · **Milestone:** M7 — Notifications, digest, and calendar
**Decision:** D-32 (`docs/specs/bl-consumer-pivot-project-spec.md` §5 Delivery)
**Origin:** NEU-1380's pre-ship review, which found the "slips flagged in copy" clause unimplemented
**Target repo:** upcoming-movies-backend · **Related, both merged:** NEU-1379 (notify pass), NEU-1380 (alert sender)

## What to build and why

D-32 admits `release_date` to the push whitelist "(assigned or moved, US theatrical or
home-release — **slips flagged in copy**)". The source spec calls date slips "the standout" of
the whole whitelist. Nothing flags one.

`_render_release_date` in `src/upmovies/synthesize/deterministic.py` is the only renderer for
the beat. It already holds `previous_date` and `new_date` and renders both directions the same:

```python
return (
    f"{market} release date moved from {_format_date(change.previous_date)} "
    f"to {_format_date(change.new_date)}."
)
```

A film delayed eight months and a film pulled forward two weeks read identically — on the feed
card and, since NEU-1380, in the alert mail, which renders `EventSummary.summary` verbatim on
purpose (`app/services/alert_sender.py`, module docstring: "the copy is the summary's"). The
mail cannot derive the flag itself without becoming a second copy of the phrasing rule, free to
drift from the card. So the flag lives in the body, which puts it on the feed card for free.

The change is one comparison and one verb. Its weight is that it is a product copy change on a
public surface, which is why the wording was agreed before shipping (below) rather than left to
the implementer.

## Key decisions

### D-1403.1 — A slip swaps the verb: "slipped from … to …"

A clause whose new date is **strictly later** than its previous date reads:

```
US wide release date slipped from 14 August 2026 to 2 October 2026.
```

A clause whose new date is earlier keeps today's wording unchanged:

```
US wide release date moved from 2 October 2026 to 14 August 2026.
```

A first date for a market (`previous_date is None`) is untouched:

```
US wide release date set to 14 August 2026.
```

Chosen over the alternatives considered:

- *"slipped" / "moved up"* — clearer for the earlier direction, but "moved up" is a US idiom
  and it widens the public copy change beyond what D-32 asks for. The reader has both dates.
- *"… a delay of seven weeks"* — names the magnitude, but needs a duration-formatting rule
  (weeks vs months, rounding) the module does not have and D-32 does not ask for.
- *"moved from … to … (a slip)"* — reads as a system annotation, not news copy.

The sentence shape, the market prefix and `_format_date` are unchanged, so the only string that
moves is the verb in the later-date branch.

### D-1403.2 — "Later" is decided per clause, never per group

`ReleaseDatesChanged` renders one clause per market, in diff order, and the markets in one
observation need not move the same way: US wide can slip while US digital moves up. Each clause
compares its own pair of dates. There is no group-level "delayed" verdict, no summary sentence,
and no reordering of clauses by direction. A mixed group therefore reads, e.g.:

```
US wide release date slipped from 17 December 2027 to 15 January 2028. US digital release date moved from 1 March 2028 to 15 February 2028.
```

### D-1403.3 — Strictly later; an equal date is not a slip

The comparison is `new_date > previous_date`. The sweep only sets `previous_date` on rows whose
change is `moved` (`ingest/sweep/release_events.py:render_change`), so an equal pair should not
reach the renderer; if one ever does, it renders with "moved", which is at least not a lie.
No guard, no exception — the renderer stays pure and total.

### D-1403.4 — The rule applies to every region and bucket alike

One template covers `limited`, `wide`, `digital` and `physical`, and origin-country regions as
well as `US` (the module exists to keep that so). The slip verb follows the same template: a
`GB limited` slip reads "GB limited release date slipped from … to …". No per-bucket or
per-region phrasing.

### D-1403.5 — `TEMPLATE_VERSION` moves to `deterministic-6`

The module's contract: bump whenever a template changes wording, so a body can be traced to the
phrasing that produced it. The pinned unit test (`test_template_version_bumped`) moves with it.

### D-1403.6 — The mail's beat label and subject stay as they are

`beat_label` and `subject.txt` in the alert sender derive from `event_type` alone; they cannot
say "slipped" without re-deriving direction from the body or the event, which is exactly the
second copy the ticket refuses. "Release date moved" as the beat with a body that says
"slipped" is the intended division: the beat is the category, the body is the news.

### D-1403.7 — No backfill

`write_deterministic_summary` writes the row once at event creation and supersession is
one-directional (never walks an LLM or admin-edited summary back to a template). New wording
reaches events carded after deploy only. Existing cards keep "moved"; they are already published
news and a mass rewrite would also collide with `edited_at` protection. Nothing migrates.

## Changes

All in `upcoming-movies-backend`.

1. `src/upmovies/synthesize/deterministic.py`
   - `_render_release_date`: after the `previous_date is None` branch, pick the verb —
     `"slipped"` when `change.new_date > change.previous_date`, else `"moved"` — and render
     `f"{market} release date {verb} from {prev} to {new}."`. Update the docstring to name the
     rule (a slip is a strictly later date, decided per clause) and why the flag lives here
     (D-32; the mail renders the body verbatim).
   - `TEMPLATE_VERSION = "deterministic-6"`.
   - The `ReleaseDateChanged` / `ReleaseDatesChanged` dataclasses are unchanged.
2. `tests/unit/synthesize/test_deterministic.py`
   - The two existing "moved names both dates" tests (`US limited` 14 Aug → 2 Oct, `US physical`
     3 Nov → 1 Dec) both move *later*, so their expected strings change to "slipped".
   - `test_two_markets_moving_together_share_one_body` (US wide 17 Dec 2027 → 15 Jan 2028) is
     also a slip; its expectation changes accordingly.
   - Add: an earlier date renders "moved" (not "slipped"); a mixed group flags only the slipped
     clause and keeps diff order; a home-release slip uses the same verb; a first date is
     unaffected; `TEMPLATE_VERSION == "deterministic-6"`.
3. `tests/integration/ingest/sweep/test_release_events.py:127` pins the rendered body
   `"US limited release date moved from 14 August 2026 to 4 December 2026."` for a real sweep
   run. That move is later, so the expectation becomes `"… slipped from …"`. This is the one
   end-to-end assertion that the sweep writes the new wording.
4. Two other tests contain "moved from" as hand-written fixture strings, not rendered output,
   and do not break:
   - `tests/unit/mail/test_alert_template.py:15` feeds a summary straight to the `alert`
     template. Update it to the "slipped" form (1 May → 18 December is a slip) so the sample
     mail reads like a real one; the assertions are pass-through and unaffected either way.
   - `tests/integration/scripts/test_prune_primary_release_events.py:39` seeds a legacy
     unqualified body. Leave it: it describes old rows, which D-1403.7 says are not rewritten.
   `tests/integration/synthesize/test_deterministic.py` uses set-only changes and asserts on
   row state, not body text; nothing to change there.
5. `CONTEXT.md` — a **Slip** term under *Release-date events* (written alongside this spec).

No schema, no migration, no frontend change, no sender change.

## Acceptance criteria

- A `release_date` body whose new date is later than its previous date reads
  `"<region> <label> release date slipped from <prev> to <new>."`.
- A body whose new date is earlier reads `"… moved from <prev> to <new>."`, unchanged from today.
- A first date for a market still reads `"… release date set to <new>."`.
- A group with one slipped and one advanced market flags only the slipped clause, in diff order.
- The same verb applies to `digital`/`physical` and to non-`US` regions.
- `TEMPLATE_VERSION` is `deterministic-6` and the pinned test says so.
- The alert mail renders the new body verbatim: no change to `alert_sender.py` or the `alert`
  templates; the sender's pass-through tests still pass.
- Existing `event_summary` rows are not rewritten.
- Suite green.

## Out of scope / deferred

- Naming the magnitude of a slip ("a delay of seven weeks") — rejected for now, see D-1403.1.
- A direction verb for the earlier case ("moved up") — rejected for now, see D-1403.1.
- Any "slipped" wording in the mail's beat label or subject line (D-1403.6).
- Backfilling or re-rendering existing summaries (D-1403.7).
- An LLM summary that supersedes a release-date body is under no obligation to flag the slip;
  that guarantee is the summarizer's, as the sender's docstring already notes.
