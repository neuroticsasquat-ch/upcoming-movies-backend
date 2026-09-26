# NEU-1470 — The digest is the only delivery (backend half)

**Ticket:** NEU-1470 "Why did I get two emails this morning?" (bl: Maintenance)
**Repos:** `upcoming-movies-backend` (this file) and `upcoming-movies-frontend`
(`frontend/docs/specs/NEU-1470-digest-is-the-only-delivery.md`). One ticket, two PRs, both on
`release/v1.1.1`; they ship together at the cutover.
**Decision record:** ADR-0021 `docs/adr/0021-the-digest-is-the-only-delivery.md` (written in
the planning session, 2026-09-26, with the glossary and the superseded-decision annotations —
carry those working-tree edits in the PR).
**Supersedes:** D-32, D-36, D-44 (Consumer Pivot); EF-7's push halves, EF-8 to EF-11 (Entity
Follows); ADR-0019 decisions 2 and 3's push clauses; project spec Digest Content §8 "alert
mail content" (moot).

## 1. What and why

Tom followed *Clayface*. A trailer was carded on 25 Sep. The `notify` slot mailed an **alert**
that night (D-32: `trailer` is on the title-follow push whitelist) and the `digest daily` slot
mailed the same trailer the next morning as a line under the film's entry. Two mails for one
beat, by design (`app.notification`: "different deliveries of the same news").

Decision (ADR-0021): **the digest is the only delivery**. No alert mail, no Web Push, no
per-beat preference. A beat reaches a reader at the cadence they chose (daily, weekly, off)
and no sooner. The digest itself does not change: it already carries every card the timeline
carries, dated, ranked and attributed (DC-1 to DC-17).

This is a removal ticket. The planning session settled, in order:

| # | Question | Answer |
|---|---|---|
| Q1 | Push, which shares the alert decision | **Retired too.** No interrupt channel remains. |
| Q2 | `alert_stores` (D-44) | **Dropped.** Column, setting, API field, UI section. The digest stays "everything the timeline carries" (DC-15). |
| Q3 | Default cadence | **Weekly stays.** No migration of stored cadences. |
| Q4 | Historic `alert` / `push` rows | **Deleted**, constraints tightened, `push_subscription` dropped. |
| Q5 | Digest markers for former alert beats | **No change to the digest.** |
| Q6 | Ticket shape | **One ticket, both repos**, two PRs. |
| Q7 | ADR | **ADR-0021**, written. |

## 2. Acceptance

1. The `notify` slot is the decision pass alone: it queues `digest` rows and sends nothing.
   `python -m upmovies.pipeline_run notify` runs without `MAIL_*` configured (it no longer
   mails) and records a detail line with no `alerts` / `push` clauses.
2. `app.notification` admits only `kind = 'digest'`, `channel = 'email'`; every pre-existing
   `alert`-kind and `push`-channel row is gone after the migration. `app.push_subscription`
   and `app.user_settings.alert_stores` (with `ck_user_settings_alert_stores`) no longer
   exist. Alembic head is the new revision; `test_migrations` passes.
3. `GET /me/settings` returns no `alert_stores`; `PATCH /me/settings` accepts
   `{digest_cadence}` only and answers 422 to an `alert_stores` key exactly as it answers any
   unknown key today (whatever the DTO's `extra` policy is — do not add a special case).
4. `/me/push` and `/me/push/vapid-public-key` are gone (404 from the router table, not 503).
5. No `alert` template exists; `available_templates()` no longer lists it; `validate_templates()`
   at boot still passes.
6. The digest sends exactly as before: `tests/integration/app/test_digest_sender.py` and
   `tests/unit/mail/test_digest_template.py` pass unchanged except where they seeded `alert`
   or `push` rows (§4.9).
7. `pywebpush` is no longer a dependency; `VAPID_*` settings no longer exist in `config.py`
   or `docker-compose.prod.yml`.
8. Pyright, ruff and the full pytest suite pass. No source file imports
   `alert_sender`, `push_sender`, `push_service`, `push_subscription_repo` or `upmovies.push`.

## 3. Design

### 3.1 The notify pass keeps one branch

`app/services/notify_service.py` keeps: the shared docstring paragraphs (two-branches-one-clause
becomes one-branch-one-clause: the digest set *is* `follow_scope`), `NOTIFY_RUN_KIND`,
`EMAIL_CHANNEL`, `Decision`, `NotifyResult` minus `alerts_queued` / `push_alerts_queued`,
`Recipient` minus `alert_stores` / `has_push`, `deliverable_events(since)` **without**
`include_upgrades` (the `updated_at` reopened window was the alert branch's; the digest reads
`created_at` only, as before), `load_recipients` without the `UserSettings` outer join and the
`PushSubscription` EXISTS, `digest_event_ids`, `queue_decisions`, `decide_for_user`,
`run_notify_pass`, `notify_detail`.

Delete: `ALWAYS_ON_ALERT_TYPES`, `NOW_AVAILABLE_EVENT_TYPE`, `PUSH_CHANNEL`,
`ATTACHMENT_PUSH_TYPES`, `SEED_GRADE_TITLE_TYPES`, `TITLE_PUSH_TYPES`, `ENTITY_PUSH_TYPES`,
`PUSH_TYPES`, `ALERT_STORE_BY_MONETIZATION`, `now_available_matches_stores`,
`names_a_seed_grade_credit`, `confirmed_enough_to_push`, `alert_reaches`, `alert_event_ids`,
and the imports that die with them (`follow_reach`, `seed_grade_credit_clause`,
`DIRECTOR_JOB`/`WRITER_JOBS`, the five catalog event-type constants, `sql_normalized_name`,
`FilmCredit`/`FilmCreditChange`/`Person`, `DEFAULT_ALERT_STORES`, `PushSubscription`,
`UserSettings`). `follow_reach` stays in `follow_queries` (`follow_scope` uses it); rewrite its
docstring (`follow_queries.py:386-417`) so it no longer describes an alert branch.

`Decision` stays a `(event_id, kind, channel)` triple with both constants fixed: the unique
key is unchanged and the row shape is not this ticket's to redesign.

The stale "nothing writes that flip yet" paragraphs (43-47, 303-310) go with the arm they
described. `ingest/sweep/confirm_events.py` **stays** — it also supersedes contradicted attach
cards (D-1446.5) — with its docstring rewritten: the confirmation flip is now a state change
on the card that the timeline and the digest's Unconfirmed pill reflect, not a delivery
trigger. Same for `news/attachment_confirm.py:54`, `pipeline_run.py:381`, and the three
lines in `tests/integration/ingest/sweep/test_confirm_events.py` that cite the push.

### 3.2 `alert_sender.py` is deleted; its shared helpers move into the digest

The digest imports `BEAT_LABELS`, `EMAIL_CHANNEL`, `JUSTWATCH_EVENT_TYPE`, `LEAD_POSTER_SIZE`,
`POSTER_SIZE` (by default argument), `film_url`, `poster_url`, `settings_url`, `mark` from
`alert_sender` (`digest_sender.py:102-111`). Move those nine names into `digest_sender.py`
(the digest is their only remaining reader; a new "shared mail helpers" module would be a
module with one importer). `BEAT_LABELS` folds into `DIGEST_BEAT_LABELS` directly. `mark`'s
docstring loses its alert framing. `EMAIL_CHANNEL` lives in `notify_service` already; import
it from there rather than keeping two. Update every docstring in `digest_sender.py` that
points at `alert_sender` (lines 6, 58, 214-216, 520, 570, 599, 651, 1187, 1325, 1372, 1402)
and the two comment-only mentions in `synthesize/deterministic.py:341,584`.

### 3.3 Push is removed whole

Delete `app/services/push_sender.py`, `app/services/push_service.py`,
`app/repos/push_subscription_repo.py`, the `upmovies/push/` package, `routers/push.py`, the
`PushSubscription` model (`models.py:576-625`), DTOs `dto.py:481-526`, `config.py:645-661`
(`vapid_*`), `docker-compose.prod.yml:198-214`, `pyproject.toml:34-38` (`pywebpush`), and
`main.py:40,145`. Tests: `tests/integration/app/test_push_sender.py`,
`tests/integration/routers/test_push.py`, `tests/unit/push/`.

### 3.4 `alert_stores` is removed whole

`models.py`: `ALERT_STORES`, `DEFAULT_ALERT_STORES` (175-176), the check constraint (478-481),
the column (491-493), the `UserSettings` docstring (442-451) and the `Follow` docstring lines
that mention it (186, 196-199). `dto.py`: `AlertStore` (233), `normalise_alert_stores`
(307-310), `UserSettingsOut.alert_stores` (450-452), `UserSettingsUpdateRequest.alert_stores`
and `_normalise` (468-478; the `cast` import goes with it); the "nothing given" rule reduces
to "`digest_cadence` is required" — keep the 422 for an empty body.
`settings_service.py:19-22,60,64-66,74-75`, `user_settings_repo.py:55-58`
(`set_alert_stores`), `routers/user_settings.py:1-2,45`.

### 3.5 The migration

One revision on `de5bd1b49fe2`, in this order:

1. `DELETE FROM app.notification WHERE kind = 'alert' OR channel = 'push'`.
2. Replace `ck_notification_kind` with `kind IN ('digest')` and `ck_notification_channel`
   with `channel IN ('email')` (`NOTIFICATION_KINDS = ("digest",)`,
   `NOTIFICATION_CHANNELS = ("email",)` in `models.py:436-437`; the unique index stays as is).
3. `DROP TABLE app.push_subscription`.
4. Drop `ck_user_settings_alert_stores`, then the `alert_stores` column.

Downgrade recreates the table and the column with their original definitions (the rows are
gone; say so in the docstring). `tests/integration/test_migrations.py:276-283` asserts the
column exists at head — flip it to assert absence, and add the constraint check the
`test_notification_model` vocabulary test already implies.

### 3.6 `pipeline_run.py`

`run_notify_stage` (547-648) keeps phase 1 and `finalize_run`; phases 2 and 3, the
`MailGateway` context, `push_configuration_problem` (522-544) and the push/alert imports
(58-59, 68, 106-110) go. `aborted` and the error chain reduce to the decision pass's.
`_NO_MODEL_CALL_MODES` (715-719) gains `"notify"`, and the `validate_mail_configuration` guard
at 888-896 no longer covers `notify` — the digest slots keep it (they are the ones that mail).
Module docstring 29-35 accordingly. `tests/integration/test_pipeline_run.py`: delete the alert
tests (1251, 1272, 1290), the push tests (1309-1441 incl. `_register_browser`), the two stubs
(1192, 1204) and the imports (11, 12, 15); edit 1217's expected detail string; rewrite 1520 to
assert the notify arm **does not** require mail configuration.

### 3.7 Templates

Delete `mail/templates/alert/`. `tests/unit/mail/test_templates.py:52-62` drops `"alert"` from
the expected tuple. `tests/unit/mail/test_alert_template.py` is deleted.

### 3.8 Docs in this repo

- `AGENTS.md`: row 38 (notify = decision pass only); section 66-114 rewritten — the notify
  slot no longer sends mail, so `MAIL_*`, `PUBLIC_BASE_URL`, `TMDB_IMAGE_BASE` become the
  **digest** slots' prerequisites (118-120 says "share the notify slot's" today — invert it);
  delete the push half (90-108), the settings-link prerequisite (86-89), and the
  "alerts lost" paragraph (109-114) becomes a digest note or goes. Cold-start note (74-78)
  stays with "alerting" → "queueing".
- `CLAUDE.md:62-64`: "notify is … the alert send that follows it" → decision pass only.
- `CONTEXT.md`, ADR-0019, Consumer Pivot D-32/D-36/D-44, Entity Follows EF-7..11: already
  edited in the planning session (working tree). Also annotate D-35 (273-275, "on the push
  whitelist"), D-39 (307-308, `/me/push`), the M7 contract list (500-510) and the Coolify
  `VAPID_*` line (586) with *(removed by ADR-0021)*; Digest Content spec lines 46, 61, 86,
  267, 368-369 the same way. Older per-ticket specs are history; leave them.
- Prose-only mentions of "alert"/"push whitelist" in docstrings (`entitlements.py:5`,
  `reset_service.py:101`, `follow_service.py:4`, `mail/types.py:91`, `news/visibility.py:10`,
  `ingest/videos.py:318`, `ingest/providers.py:153`, `ingest/imports/apply.py:12,71`,
  `letterboxd.py:8`, `tmdb_account.py:11`, `credit_history.py:13,20`, `company_events.py:34`,
  `org_pipeline.py:30,293`, `tiebreak.py:84,127`, `public/service.py:551`,
  `tests/fixtures/public.py:183-184`): reword to "digest" or "timeline" where the sentence
  is about delivery; leave "alert window" alone (the glossary keeps that name).

### 3.9 Tests to rewrite rather than delete

- `tests/integration/app/test_notify_pass.py`: delete the push tests (1255-1441) and the
  alert-only tests (400, 417, 441, 468, 495, 534, 564, 639, 718, 770, 810, 849, 888),
  `_set_alert_stores` (162-170), imports 19 and 36. Every remaining test that asserted a
  `kind="alert"` row beside its digest row asserts the digest row alone. Keep the
  suppression, idempotency, watermark and visibility tests — they are the pass's contract.
- `tests/unit/app/test_notify_decisions.py`: keep 151-155 and 172-184, edit 158-169 (detail
  line), delete the rest.
- `tests/integration/app/test_notification_model.py`: fixture default `kind="digest"` (44);
  64-75 becomes "one row per `(user, event)` — a second `digest/email` insert conflicts".
- `tests/integration/app/test_digest_sender.py:1074-1087`: the "not this pass's work" test
  can no longer seed `alert`/`push` rows; replace with a `status != 'queued'` sibling or drop
  it if `test_digest_sender` already covers that.
- `tests/integration/routers/test_settings.py`: 52, 103-151 (D-44 block), 38-40, and the
  parametrised 422 cases that used `alert_stores` payloads.

## 4. Out of scope / deferred

- The digest's content, cadence, slate day and `off` semantics: unchanged (DC-1..DC-17).
- Renaming **alert window** (`ALERT_WINDOW_DEAD_STATUSES`, `alert_window_clause`): the
  glossary records why the name stays.
- Per-user time zones, a "daily" default, digest markers for big beats: declined (Q3, Q5).
- The `notify` and digest Coolify slots and healthchecks: unchanged. `VAPID_*` variables can
  be removed from Coolify at leisure; nothing reads them after this deploys.

## 5. Deploy notes

- Both PRs land on `release/v1.1.1`; the cutover deploys them together. Order does not matter
  for correctness (the frontend's settings page stops calling `/me/push` in the same
  release), but if they ever deploy apart, frontend first.
- The migration deletes queued-but-unsent `alert` rows. Nothing is lost: every such event
  also has a `digest` row.
- Coolify: no new variables. Remove `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`
  when convenient.
