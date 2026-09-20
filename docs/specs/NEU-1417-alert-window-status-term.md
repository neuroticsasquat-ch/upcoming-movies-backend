# NEU-1417 — The alert window's status term: an indirect follow covers a released film

**Target repo:** upcoming-movies-backend

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1417
**Story:** NEU-1413 *Follow subsumes the watchlist*
**Milestone:** M8 — Follow subsumes the watchlist
**Related:** NEU-1414 (shipped the window this ticket corrects; PR #348)
**Decisions:** D-46 (new, project spec), ADR-0018 amended; D-1414.2 superseded in part.
D-11, D-27, D-28, D-43, D-45 unchanged.

## What to build and why

NEU-1414 shipped `catalog.queries.alert_window_clause` as `in_play_clause` with the date bound
moved back by `PROVIDER_POLL_MAX_AGE_DAYS` **and the same status term**. Because
`TMDB_EXCLUDED_STATUSES` defaults to `Released,Canceled`, a person, company or franchise follow
stops covering a film the day TMDB marks it `Released`. The spec's own rationale for the window
("a film that opened last month is still owed its `now_available` beat") is the thing the
acceptance criterion ("one whose status is excluded is not, whatever its date") took away.

Decided in the planning session on 2026-09-20:

1. **The alert window gets its own status term: only `Canceled` is out.** `Released` rides
   the date bound like every other status. An indirect follow covers a film from announcement
   until `PROVIDER_POLL_MAX_AGE_DAYS` after its primary release date, whatever TMDB's status
   says short of the film being called off. This is what D-1414.2's rationale described.
2. **`PROVIDER_POLL_MAX_AGE_DAYS` becomes 365** and stops being a placeholder. The default
   alert store is `stream`, so the window has to reach the streaming debut, not the digital one;
   studio pay-1 streaming windows run 45 to about 240 days after theatrical, and foreign titles
   reach US streaming later still. A year covers them with margin, and the measurement below
   shows it costs the poll nothing today.

### Ground truth, measured on the local catalog (a prod refresh, 2026-09-20)

| Population | Count |
|---|---|
| Films in the catalog | 9,550 |
| Primary date in the last 200 days | 919 |
| ...of which `Released` | 380 |
| ...of which `Post Production` / `In Production` / `Planned` (status lagging the date) | 539 |
| Films with a primary date older than 200 days, any status | 13 |
| Rule-1 poll set (US theatrical governing date 14–200 days old) | 97 |
| Rule-1 poll set at 14–365 days | 98 |
| Films with a primary date in the last 365 days and not `Canceled` (rule-2 ceiling) | 924 |

Three things this settles:

- **The "back catalogue" the date bound guards against does not exist in the catalog.**
  Admission skips `Released` films at ingest (`ingest/tmdb/filters.py::classify_skip`), so
  a film is only ever in the catalog because it was admitted *before* release and aged in
  place. A company follow cannot reach twenty years of output; there are 13 films older than
  200 days. The date bound still matters as the catalog ages, but it is a ceiling on growth,
  not a defence against a flood that is already there.
- **What the status term was throwing away is the whole M6/M7 home-release beat.**
  `now_available` cards (D-28), the `US:digital` / `US:physical` `release_date` events (D-26)
  and late trailers (D-35) all land on films TMDB has marked `Released`. Those are the beats
  the alert window exists to deliver, and the 380 `Released` films are precisely the ones with
  real US releases. The 539 status-lagging films the window did reach are the festival and
  foreign titles TMDB has not caught up on.
- **Rule 1 already polls `Released` films with no status filter.** The provider poll is not
  a place where `Released` means "stop looking"; it is the state in which looking pays.
  Widening rule 2 to match adds at most the followed subset of 924 films, on top of a rule-1
  set of ~98, at two TMDB requests per film per day. That is not the slot's runtime problem.

## Design

### D-1417.1 — The alert window's own status term, as a constant

`catalog/queries.py`:

```python
ALERT_WINDOW_DEAD_STATUSES: frozenset[str] = frozenset({"Canceled"})
"""The one TMDB status past which no follow is owed anything about a film. `Released` is not
here on purpose: it is the state in which the home-release beats happen (D-1417.1, D-46)."""


def alert_window_clause(*, today: date, max_age_days: int) -> ColumnElement[bool]:
    return and_(
        or_(Film.release_date.is_(None), Film.release_date >= today - timedelta(days=max_age_days)),
        or_(Film.status.is_(None), Film.status.not_in(ALERT_WINDOW_DEAD_STATUSES)),
    )
```

- The `excluded_statuses` parameter is **removed** from `alert_window_clause`, not defaulted.
  TMDB's status vocabulary is closed (`Rumored`, `Planned`, `In Production`,
  `Post Production`, `Released`, `Canceled`); the only dead one is `Canceled`, and there is
  nothing to tune from Coolify. A constant with no parameter is also the shape that makes it
  impossible to hand an alert-window builder the in-play set by mistake, which with ten call
  sites was the live risk of a second setting.
- `TMDB_EXCLUDED_STATUSES` is untouched. It keeps governing admission (`classify_skip`),
  `in_play_clause`, `active_film_clause`, the sweep's enumerate and refresh phases, and the
  D-11 timeline builder `followed_film_ids`. D-11 timeline coverage stays the in-play cut
  (the ticket's constraint; a person's back catalogue would otherwise flood the timeline).
- The clause's docstring is rewritten: the "Known limit" paragraph goes; in its place, why the
  status term differs from in-play's (`Released` is where the beats land; `Canceled` is the
  only state with nothing left to deliver) and why it is a constant rather than a setting.
  The "why any bound at all" paragraph is corrected: admission keeps the back catalogue out of
  the catalog, so the date bound is a ceiling on how long a followed film keeps costing a poll
  and keeps a place on the watchlist, not a guard against an existing flood.

**The parameter leaves every builder that only threaded it to the window.** These callers
drop `excluded_statuses` from their signatures and their call sites:

| Function | Change |
|---|---|
| `follow_queries.covered_film_ids`, `watchlist_film_ids`, `covering_follows`, `covered_by_any_user_clause` | drop `excluded_statuses`; `followed_film_ids` **keeps** it (in-play, D-11) |
| `ingest/providers.py::poll_set_clause`, `load_poll_set`, `run_provider_poll` | drop `excluded_statuses` (it was only passed through to rule 2) |
| `ingest/videos.py::run_video_poll` | drop `excluded_statuses` |
| `pipeline_run.run_providers_stage` | stop passing it to both polls; `run_notify_stage` keeps passing it (the digest branch is D-11) |
| `notify_service.alert_event_ids` | drop `excluded_statuses`; `decide_for_user` / `run_notify_pass` keep it for `digest_event_ids` and stop forwarding it to the alert branch |
| `digest_sender.load_slate` | stop passing it to `watchlist_film_ids` |
| `public/service.py` `_calendar_governing_cte(watchlist_user_id=)` and `get_ical_feed` | stop passing it; `get_timeline` keeps it |
| `watchlist_service._window()` | returns `(today, max_age_days)` |

`covered_by_any_user_clause`'s docstring and `poll_set_clause`'s "Rule 2" paragraph lose the
"at that follow's coverage, inside the alert window" caveat about status; `videos.py`'s module
docstring paragraph beginning "M8 widened that rule" gets one sentence: a released film a
person follow reaches is polled until the window's far end, which is the trailer-after-release
and streaming-debut case the poll was missing.

### D-1417.2 — `PROVIDER_POLL_MAX_AGE_DAYS = 365`, no longer a placeholder

- `config.py`: default `365`. The comment block loses "Both are placeholders in the §4.5
  sense" and says instead why 365: the default alert store is `stream`; pay-1 streaming debuts
  run 45 to ~240 days after theatrical and foreign titles later; the rule-1 set measured the
  same size at 365 as at 200 because admission keeps released films out of the catalog, so the
  ceiling is cheap; `PROVIDER_POLL_MIN_AGE_DAYS` stays 14 and stays a placeholder (nothing
  measured it).
- `docker-compose.prod.yml`: the fallback becomes `${PROVIDER_POLL_MAX_AGE_DAYS:-365}`.
- **Deploy note (AGENTS.md's tuned-constants checklist):** Coolify shadows compose fallbacks.
  After merge, set `PROVIDER_POLL_MAX_AGE_DAYS=365` in the Coolify UI if the variable is
  stored there, restart, and verify with `printenv` on the running container. Until that is
  done prod runs the old ceiling and the window is 200 days wide, which is safe but not what
  was decided. Add one line to AGENTS.md's tuned-constants gotcha naming this variable.

### D-1417.3 — The decision, recorded once and reflected everywhere the old rule was

- **`docs/specs/bl-consumer-pivot-project-spec.md`**: new **D-46** under *Follow subsumes the
  watchlist*, after D-45:

  > **D-46 The alert window ends at `Canceled`, not at `Released`.** A person, company or
  > franchise follow covers a film from announcement until `PROVIDER_POLL_MAX_AGE_DAYS` (365)
  > after its primary release date, in every TMDB status but `Canceled`. `Released` is the
  > state the home-release beats (D-26, D-28, D-35) land in, so a window that ended there
  > delivered none of them to an indirect follower. `TMDB_EXCLUDED_STATUSES` keeps governing
  > admission and the in-play working set; the window's own term is a constant
  > (`catalog.queries.ALERT_WINDOW_DEAD_STATUSES`), because TMDB's vocabulary is closed and
  > there is nothing to tune. Title follows are unchanged: any state. (NEU-1417, 2026-09-20.)

  The M8 shared-contracts bullet ("status not excluded and primary release date NULL or no
  older than…") is reworded to "not `Canceled`, and primary release date NULL or no older than
  `PROVIDER_POLL_MAX_AGE_DAYS` (365)".
- **`docs/adr/0018-follow-subsumes-the-watchlist.md`**: the "Resolved in NEU-1414's planning"
  paragraph gains an amendment sentence, in the style of D-45's: *(Amended 2026-09-20 in
  NEU-1417: the window's status term is its own, `Canceled` only; `Released` rides the date
  bound, since it is the state the home-release beats land in. D-46.)*
- **`docs/specs/NEU-1414-computed-watchlist-over-follows.md`**: D-1414.2 gets a one-line
  *Superseded in part by NEU-1417 (D-46)* note at its head; the acceptance criterion "one
  whose status is excluded is not, whatever its date" is annotated the same way. The
  "Out of scope" bullet on tuning `PROVIDER_POLL_MAX_AGE_DAYS` is annotated as done here.
- **`CONTEXT.md`**, **Alert window**: rewritten. From announcement until
  `PROVIDER_POLL_MAX_AGE_DAYS` after the primary release date, in any status but `Canceled`.
  Wider than in play in *two* ways now, both deliberate: the date bound is moved back, and
  `Released` is inside, because it is the state in which the beats the window exists for
  happen. The sentence "Note the status term is the *same* one in play applies…" goes. Keep
  the title-follow sentence. Add to _Avoid_: "in play's status term". The **Watchlist** entry
  needs no change.
- **`AGENTS.md`**: one line under the tuned-constants gotcha (D-1417.2).

### D-1417.4 — Tests pin the new boundary

- `tests/integration/app/test_follow_queries.py::test_the_alert_window_bounds_an_indirect_follow`:
  `MAX_AGE_DAYS = 365`; the `(TODAY - 1 day, "Released", False)` case becomes `True`, and
  gains a sibling `(TODAY - MAX_AGE_DAYS, "Released", True)` and
  `(TODAY - MAX_AGE_DAYS - 1, "Released", False)` so the far end is pinned on the status that
  matters; `Canceled` stays `False` with a future date. Docstring rewritten: the case to read
  twice is now that `Released` is *in*. `_covered` and `EXCLUDED` lose their alert-window use
  (`EXCLUDED` stays for `followed_film_ids`).
- `test_follow_queries`: one new test that a `Released` film inside the window is **not** on
  the timeline through the same person follow (`followed_film_ids` is still in-play), so the
  two builders' deliberate difference is pinned rather than assumed.
- `tests/integration/ingest/test_providers.py` and `test_videos.py`: `MAX_AGE = 365`,
  `EXCLUDED` and its docstring removed with the parameter; one providers test that a
  `Released` film 250 days past its primary date with no US theatrical row is polled when a
  person follow at `lead` reaches it, and not when nobody does (the new reach, at a date the
  old ceiling would have cut).
- `tests/integration/app/test_notify_pass.py`: `MAX_AGE_DAYS = 365`; one test that a
  `now_available` card on a `Released` film reaches a user who follows only its director
  (this is the alert the ticket is about). `EXCLUDED` stays for the digest branch.
- `tests/integration/test_pipeline_run.py`: the `provider_poll_max_age_days: 120` overrides
  are unaffected; the providers-stage call assertions drop `excluded_statuses`.
- `tests/unit/test_config.py`: alias table unchanged; if a default is asserted anywhere,
  365.

### D-1417.5 — Poll-set sanity check before the PR opens

The "if the window widened" clause of the ticket's Done-when. Against a fresh
`task db:refresh` (catalog/news/ingest from prod), run the new `poll_set_clause` at 365 days
through a one-off `select(func.count()).where(...)` in a scratch script (or the equivalent
SQL) and record in the PR body: the rule-1 count, the rule-2 count, and the union. Compare to
the numbers above (97 / 98 rule 1; rule 2 bounded by 924 times the fraction any follow
reaches). The local follow graph is one title follow, so rule 2 will read small; the number
that matters is rule 1 plus the ceiling, and both are already known to be a few hundred at
most. No deadman grace change is expected.

### What does not change

- D-11 timeline coverage: `followed_film_ids` keeps `in_play_clause` and
  `TMDB_EXCLUDED_STATUSES`. A released film a person follow reaches is on the watchlist and
  not on the timeline; its home-release *cards* reach the timeline only through the film's
  own events being feed-visible, as today.
- Title follows cover any state. `Canceled` films are still covered by a title follow.
- `coverage ∈ lead|all` and its credit cuts; mutes; the entitlement gate; the push whitelist;
  `PROVIDER_POLL_MIN_AGE_DAYS`; `TMDB_EXCLUDED_STATUSES` and every consumer of it.
- `alert_window_clause`'s NULL guards, `correlate(None)` on the builders, the SELECT-list
  casts.

## Acceptance criteria

### `catalog/queries.py`, `app/follow_queries.py`

- `alert_window_clause(today=, max_age_days=)` has no status parameter; a film with status
  `Released` and a primary date `today - max_age_days` is inside; one a day older is outside;
  `Canceled` is outside whatever its date; NULL date and NULL status are inside.
- `covered_film_ids`, `watchlist_film_ids`, `covering_follows`, `covered_by_any_user_clause`
  take no `excluded_statuses`; `followed_film_ids` still does and still applies in-play.
- A `Released` film inside the window is covered through a person follow at `lead` (director
  credit) and is *not* returned by `followed_film_ids` for the same follow.

### `ingest/providers.py`, `ingest/videos.py`, `pipeline_run.py`

- `poll_set_clause`, `load_poll_set`, `run_provider_poll`, `run_video_poll` take no
  `excluded_statuses`; the providers stage passes `max_age_days` from settings and nothing
  about statuses.
- A `Released` film 250 days past its primary date with no US theatrical row is in the poll
  set when any user's person follow at `lead` reaches it and that user has not muted it, and
  out when nobody covers it.

### `notify_service.py`, `digest_sender.py`, `public/service.py`, `watchlist_service.py`

- A `now_available` card on a `Released` film alerts a user who follows only its director
  (store `stream` in their default `alert_stores`).
- The digest slate, `/me/calendar`, the `.ics` feed and `GET /me/watchlist` all show a
  `Released` film a company follow reaches, up to 365 days after its primary date.
- The notify pass's digest branch still passes `TMDB_EXCLUDED_STATUSES` to `followed_film_ids`.

### Configuration and deploy

- `settings.provider_poll_max_age_days` defaults to 365; `docker-compose.prod.yml` fallback
  is 365; the config comment no longer calls it a placeholder; AGENTS.md names it in the
  tuned-constants checklist.

### Documents

- D-46 in the project spec, M8's contract bullet reworded, ADR-0018 amended, D-1414.2 and its
  acceptance criterion annotated, `CONTEXT.md`'s **Alert window** entry rewritten, and the
  docstrings on `alert_window_clause`, `covered_by_any_user_clause`, `poll_set_clause` and
  `videos.py` agree with all of the above. Nothing left in the repo says a `Released` film
  leaves an indirect follow's coverage.

### Tooling

`task format`, then `task test && task lint && task typecheck` green, run once in the
foreground in the api container against `app_test`. Poll-set numbers recorded in the PR body
(D-1417.5).

## Out of scope / deferred

- **Tuning `PROVIDER_POLL_MIN_AGE_DAYS`** (14). Still a placeholder; nothing here measured
  it.
- **Re-tuning the ceiling from observed lags.** `catalog.availability_first_seen` holds no
  rows locally and only 20 films carry a US digital date; when provider observations exist,
  the theatrical-to-first-`stream` lag distribution is the number to set 365 against.
- **Timeline coverage of released films** (D-11's in-play cut). The ticket's constraint; a
  separate decision if anyone wants a director's just-released film on the timeline.
- **The Coolify variable flip** is a deploy action, not code; it is called out in D-1417.2
  and must be done after merge for the decision to take effect in prod.
- **`in_play_clause`'s "nothing else should reach for it" docstring** already lists the
  refresh phase and the D-11 builder implicitly; no change.
