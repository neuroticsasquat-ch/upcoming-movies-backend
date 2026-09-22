from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The host a stage's model is served by — the gateway's second axis, alongside the per-stage
# model settings that were already here (design §8). A `Literal` rather than a bare `str` so a
# misspelled provider fails the container at boot rather than the stage at its first call: the
# same discipline `rates_for` applies to an unknown `(provider, model)`.
#
# The names are duplicated from `llm.registry.PROVIDERS` rather than imported. `llm.gateway`
# reads `Settings`, so importing the `llm` package here would be a cycle — the same bind the
# retrieval constants below are in, and a test pins the two together the same way.
Provider = Literal["anthropic", "deepinfra", "deepseek"]

# Which provider sends transactional mail (D-30). A `Literal` for the same reason `Provider`
# is one — a misspelled provider fails the container at boot rather than the signup that
# needed the mail — and duplicated from `mail.registry.MAIL_PROVIDERS` rather than imported,
# because `mail.gateway` reads `Settings` and importing the `mail` package here would be a
# cycle. A test pins the two together, exactly as it does for `Provider`.
MailProvider = Literal["resend", "noop"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(..., alias="DATABASE_URL")
    test_database_url: str | None = Field(default=None, alias="TEST_DATABASE_URL")
    admin_token: str = Field(..., alias="ADMIN_TOKEN")

    tmdb_api_key: str = Field(..., alias="TMDB_API_KEY")
    tmdb_base_url: str = Field(default="https://api.themoviedb.org/3", alias="TMDB_BASE_URL")
    tmdb_rate_limit_requests: int = Field(default=40, alias="TMDB_RATE_LIMIT_REQUESTS")
    tmdb_rate_limit_window_seconds: int = Field(default=10, alias="TMDB_RATE_LIMIT_WINDOW_SECONDS")
    tmdb_retry_max_attempts: int = Field(default=5, alias="TMDB_RETRY_MAX_ATTEMPTS")

    # Where themoviedb.org sends the user back after they approve (or refuse) the import's
    # request token (D-16). A frontend page, not a route here: TMDB appends `request_token` and
    # `approved` as query params to whatever this names, and the page reads them and calls
    # `POST /me/import/tmdb/callback` with the session cookie and the CSRF header the route
    # requires. Pointing it straight at the API would arrive as a bare cross-site GET with
    # neither.
    tmdb_redirect_url: str = Field(
        default="http://localhost:5173/welcome?tmdb=callback", alias="TMDB_REDIRECT_URL"
    )

    # Rolling release-date window + filters for the TMDB discover ingestion.
    tmdb_release_window_past_days: int = Field(default=0, alias="TMDB_RELEASE_WINDOW_PAST_DAYS")
    tmdb_release_window_future_days: int = Field(
        default=1095, alias="TMDB_RELEASE_WINDOW_FUTURE_DAYS"
    )
    tmdb_min_popularity: float = Field(default=1.0, alias="TMDB_MIN_POPULARITY")
    tmdb_min_runtime: int = Field(default=60, alias="TMDB_MIN_RUNTIME")
    tmdb_excluded_statuses_raw: str = Field(
        default="Released,Canceled", alias="TMDB_EXCLUDED_STATUSES"
    )

    # How long an undated film may stay quiescent — no `catalog.film_field_change` row and
    # no linked story — before `active_film_clause` drops it from the working set (ADR-0015).
    # Placeholder: deliberately long, so almost nothing goes dormant until the M4 tuning
    # ticket sets it from the discovery probe. Erring long costs per-film queries; erring
    # short silently stops following live films.
    sweep_dormancy_days: int = Field(default=365, ge=1, alias="SWEEP_DORMANCY_DAYS")
    # How often the sweep's refresh phase still re-fetches a dormant film. Dormancy is not
    # an exemption from refreshing — detecting the change that revives a film requires
    # reading it, so a dormancy that stopped the refresh would be a one-way door (§4.5) —
    # it is a *reduced cadence*. Placeholder until the M4 tuning ticket sets it: erring
    # short only costs requests, erring long delays every revival by that much.
    sweep_dormant_refresh_days: int = Field(default=30, ge=1, alias="SWEEP_DORMANT_REFRESH_DAYS")
    # How far back the sweep's field-change phase reads `catalog.film_field_change` for events
    # to card (ADR-0014). A fixed rolling window, not a watermark: re-reading a carded change
    # is a no-op, so the overlap costs a couple of indexed queries and means a failed sweep
    # loses nothing. It is also the day-one guard — the table holds months of history, and
    # without a floor the first pass after deploy would card every date move ever recorded.
    sweep_event_lookback_days: int = Field(default=7, ge=1, alias="SWEEP_EVENT_LOOKBACK_DAYS")
    # How long a credit detachment must age before it is eligible to card, so a rapid TMDB
    # re-attachment (a flap) is observed and suppressed rather than carded as a real departure
    # (NEU-1205). Forward-dwell: a removal cards only if the person does NOT re-attach within N
    # days after it. 0 disables the gate (reverts to plain NEU-1200). Must be <
    # SWEEP_EVENT_LOOKBACK_DAYS so a held removal is still in the rolling window when it becomes
    # eligible — understated by the same rounding as the quarantine below, because a dwell is
    # only ever observed at a sweep pass and its effective hold runs up to
    # `SWEEP_HOLD_ROUNDING_HOURS` (48h) past N days. Deliberately left unguarded even so: the
    # backfill backstops a mis-tuned N above the lookback (NEU-1401, D-1401.6). At the default 3
    # days that still leaves 48h of headroom against a 7-day window. Re-verify against prod.
    sweep_credit_dwell_days: int = Field(default=3, ge=0, alias="SWEEP_CREDIT_DWELL_DAYS")
    # How long a credit *attachment* must age before it is eligible to card (ADR-0017, D-3).
    # The generalisation of the dwell gate above to the other direction: an added credit cards
    # only once it has survived the window *and* is still attached, so an edit that was never
    # true — vandalism, a misfile — publishes nothing at all rather than publishing and being
    # corrected. 0 disables the hold (reverts to immediate carding). Hours rather than days
    # because the difference between 48 and 72 is a tuning step this must be able to express —
    # *not* because the curve turns over inside a day. It does not, and NEU-1372 found that
    # cannot even be observed at a daily ingest cadence: the log is stamped almost entirely in
    # one hour a day, nothing reverts before 18h, the mass sits at 24–36h, and there is a
    # genuine heavy tail past 96h.
    #
    # Must leave room for the sweep pass that *observes* the hold, not merely fit inside
    # SWEEP_EVENT_LOOKBACK_DAYS: eligibility is only ever checked at a pass, so the effective
    # hold runs up to `SWEEP_HOLD_ROUNDING_HOURS` (48h) longer than the value set here, and the
    # ceiling is the lookback in hours *minus 48* — 120h at a 7-day window, not 167h. Unlike the
    # dwell gate, which the removal backfill backstops, nothing recovers an attachment that ages
    # out unheld, which is why this constraint is *enforced* at boot
    # (`ingest.sweep.validate_sweep_configuration`) rather than only documented (NEU-1401).
    # Default 72h. The M5 spike (D-4, NEU-1372) has since read the knee off `film_credit_change`;
    # whether to retune from its findings is a separate decision and a separate deploy.
    # Re-verify against prod.
    sweep_credit_quarantine_hours: int = Field(
        default=72, ge=0, alias="SWEEP_CREDIT_QUARANTINE_HOURS"
    )

    # How far back the Tier-A short-circuit will look to pair a trade story with a credit
    # attachment (ADR-0017, D-5; NEU-1371). Read from both ends: the cluster stage publishes a
    # film's pending attachments no older than this when a story card names the person, and the
    # sweep loader retires a pending attachment that finds a story card no older than this
    # before it. `0` disables the short-circuit in both directions, which restores the
    # pre-NEU-1371 duplicate.
    #
    # It must comfortably exceed SWEEP_CREDIT_QUARANTINE_HOURS, because the whole point is to
    # catch a story that lands while the attachment is still held: a window shorter than the
    # hold would let a change clear quarantine and card beside the story that already reported
    # it. 14 days is the 72h default plus generous slack, which is what buys the tolerance —
    # the trades commonly run a casting story days before or after TMDB records the credit.
    #
    # Not enforced at boot, unlike the quarantine/lookback pair: mis-tuning this publishes a
    # duplicate card, which is visible on the feed and fixable forward, where a hold outliving
    # its window silently publishes nothing at all and is not.
    sweep_story_confirm_days: int = Field(default=14, ge=0, alias="SWEEP_STORY_CONFIRM_DAYS")

    # The three sanity holds (ADR-0017, D-8, NEU-1370). Where quarantine asks whether an edit
    # survived, these ask whether it is possible at all, and they hold on the *person* rather
    # than on the clock. All three are `ge=1`: unlike the two gates above, none has a coherent
    # "0" — a burst threshold of zero would hold every attachment ever made, and a zero-year
    # posthumous or age bar would hold every credit of everyone TMDB holds a date for. Turning
    # one off is a code change, not a Coolify edit, which is the right cost for removing a
    # defacement check.
    #
    # How many films one person may be attached to on one observation day before every one of
    # those attachments is held as a burst. 20 is a placeholder set from the shape of the
    # attack rather than from measurement: a real person reaching twenty *new* seed-grade
    # credits inside a day is vanishingly rare, and one attachment run of a vandalised person
    # id routinely passes it. Retune against `film_credit_change` once M5 has holds in prod.
    sweep_sanity_max_films_per_day: int = Field(
        default=20, ge=1, alias="SWEEP_SANITY_MAX_FILMS_PER_DAY"
    )
    # How long after a recorded death a credit is still ordinary. Completed films, archive
    # footage and posthumous voice work all land inside a couple of years, so the check is for
    # credits arriving long after — 2 years, which is comfortably past the release lag of a
    # film that was already shooting.
    sweep_sanity_posthumous_years: int = Field(
        default=2, ge=1, alias="SWEEP_SANITY_POSTHUMOUS_YEARS"
    )
    # The studio half's one sanity check (EF-5, NEU-1433): how many films a single production
    # company may be recorded as attaching to on one observation day. Reaching the threshold is
    # enough — a company with N or more same-day attachments has every one of them withheld, the
    # `>=` `SWEEP_SANITY_MAX_FILMS_PER_DAY` already uses. D-8's shape with its own number, and
    # the number has to be a very different one — a person reaching twenty new seed-grade
    # credits in a day is vanishingly rare, while a studio genuinely picking up a slate in one
    # TMDB editing session is not.
    #
    # 100 is a placeholder set from the shape of the attack rather than from measurement, and
    # set deliberately generously because this check has no escape hatch: the credit holds keep
    # an `ingest.credit_hold` row an admin can release by hand (`ingest.credit_holds`), while
    # this one is stateless and a withheld run that never falls below the threshold simply ages
    # out of the rolling window uncarded. Retune against `film_company_change` once there is a
    # distribution to read. `0` turns the check off, which is safe here precisely because there
    # are no hold rows to strand.
    sweep_company_sanity_max_films_per_day: int = Field(
        default=100, ge=0, alias="SWEEP_COMPANY_SANITY_MAX_FILMS_PER_DAY"
    )
    # The age below which a seed-grade credit is implausible on its face. 3 rather than 5:
    # infants really are cast, and a bar set where a genuine credit lives would hold real
    # casting announcements to catch a vandal who could equally have typed 1 as 4.
    sweep_sanity_min_age_years: int = Field(default=3, ge=1, alias="SWEEP_SANITY_MIN_AGE_YEARS")

    # The theatrical half of the watch-provider poll's scoped set (D-27): a film is polled
    # while its US theatrical governing date is between these two ages, in days. The floor
    # keeps the poll off films still in their theatrical window, where a home release is not
    # yet plausible and a daily request would buy nothing; the ceiling is where a film that
    # never got one stops costing a request a day forever. A film somebody follows by title is
    # polled regardless of both — somebody is waiting on that answer (EF-14).
    #
    # `max_age_days` is also the **alert window** (`catalog.queries.alert_window_clause`,
    # D-1414.2, D-46), and the two no longer describe one quantity. EF-14 severed that: a
    # follow covers nothing by date any more, so what the window bounds now is which films an
    # entity page lists as recently released and which an import may propose (EF-21). Tuning
    # this still moves both, so it is still one number to reason about — but the second half of
    # it is a catalog cut, not a delivery rule.
    #
    # The ceiling is 365 and is **no longer a placeholder** (D-1417.2, NEU-1417). The default
    # alert store is `stream`, so the window has to reach the streaming debut rather than the
    # digital one: studio pay-1 windows run from about 45 days to about 240 days past
    # theatrical, and a foreign title reaches US streaming later still. A year covers them with
    # margin, and it costs the poll nothing measurable — admission skips `Released` films
    # (`ingest.tmdb.filters.classify_skip`), so the catalog holds no back catalogue for the
    # ceiling to let in, and the rule-1 set measured the same size at 365 as at 200 (97 vs 98
    # films, 2026-09-20). The floor stays 14 and stays a placeholder in the §4.5 sense —
    # nothing has measured it, and erring narrow there costs requests during the theatrical
    # window where a home release is not yet plausible.
    provider_poll_min_age_days: int = Field(default=14, ge=0, alias="PROVIDER_POLL_MIN_AGE_DAYS")
    provider_poll_max_age_days: int = Field(default=365, ge=1, alias="PROVIDER_POLL_MAX_AGE_DAYS")

    # The sweep's master switch, in the manner of NEWS_GOOGLE_ENABLED: off means it still
    # enumerates and still reports, but writes nothing (spec §7.3). Kept separate from the
    # three tranche flags below so a rollback is one move and does not disturb the ramp.
    sweep_enabled: bool = Field(default=False, alias="SWEEP_ENABLED")
    # Admission ramps one seed grade at a time — directors, then writers, then top-5 cast
    # (§7.4) — so the retrieval-health guard reacts to a 1,446-person expansion before a
    # 7,519-person one, and a precision drop names the grade that caused it. Every flag
    # ships false whatever the ramp has reached: which tranches are *open* is env, so
    # opening one is a Coolify change rather than a deploy, and closing it is the same move
    # in reverse. Directors went live 2026-08-11 (NEU-1086); **writers went live
    # 2026-08-12**, and the first sweep with it open admitted 407 films, 138 of them reached
    # through a writer credit with no director attached — against the +270 the pre-flip probe
    # predicted as a lower bound. Cast is still closed.
    #
    # **Writers is a supported path as of NEU-1089**, and a deliberately small step. Measured
    # on the pre-expansion probe, counting only films no director reached (spec §7.4, §4.3):
    # **+270 films**, about 12% catalog growth on top of the directors tranche's 639, at a
    # status profile close to directors' — `Rumored` is 18% against 19%, and the rest sits
    # within four points. Read it as a lower bound: the probe predates the directors flip,
    # whose admissions have since contributed 486 seed people that reach films it could not
    # see. What it buys is the earliest signal the product can get — a script with no
    # director attached to it yet.
    #
    # It costs **no additional enumerate requests**, which corrects the ticket: the seed query
    # is not tranche-scoped, so every seed person at all three grades — 7,666 of them after
    # the directors flip (§4.3) — is already enumerated on every sweep, and the writers
    # grade's 1,745 are among them. Enumerating regardless of the flags is deliberate: it is
    # what makes `withheld` and the attachment histogram an honest answer to "what would
    # opening this tranche admit". Flipping the flag adds the refresh cost of 270 more films
    # and whatever seeds they contribute back (§3.3), not a fresh grade's worth of requests.
    #
    # **No tuning constant moves with it.** The corroboration threshold was measured against
    # the *director-reached* distribution (below), and +270 films at a near-identical status
    # profile is not evidence to reopen it; T and K were re-derived at NEU-1088 over a
    # catalog this grows by ~12%, and `link/retrieval/select.py` already prices that as
    # likely moving nothing. The cast tranche is where both are expected to move.
    #
    # **Cast is a supported path as of NEU-1090**, and it is the one that stresses the system:
    # **+2,341 films**, which against a post-writers catalog of roughly 2,500 is about a 93%
    # increase — near enough to say it doubles the catalog, and note the ticket's "104%" takes
    # the *pre*-writers baseline. The same correction as above applies to its seed-count claim
    # too: cast does not add 5,299 seed people or take the set "to its full 7,519", because
    # the query is not tranche-scoped and they are all enumerated today. What doubles is the
    # refresh phase, and the rate at which admitted films contribute their own cast back.
    #
    # It is also, against the premise the ramp was designed on, the **cleanest** grade by
    # status: 7% `Rumored` against directors' 19%, and 62% already `In Production` or `Post
    # Production`. Top-billed cast attach late, so by the time casting is announced the film
    # is usually real. The §7.4 ramp order still stands, but on **retrieval-precision**
    # grounds — collision risk scales with catalog size however real the films are — not on
    # the vaporware grounds originally argued. Do not plan it expecting to raise the
    # corroboration threshold; the data points the other way. Nor to tighten the billing cut
    # (see `catalog/seed_grade.py`, where that was measured and rejected).
    #
    # **Expect it to breach the retrieval soft tier, by design** — breaching is what schedules
    # the third T/K pass rather than leaving it to be rediscovered by hand. How far past the
    # threshold is deliberately not projected here; `link/retrieval/health.py` owns that
    # reasoning, including why the obvious linear extrapolation is a floor rather than an
    # estimate. The *hard* tier is the one to actually fear, and zero-candidate falls as the
    # catalog grows.
    #
    # **Flip each one only once retrieval health is green on the tranche before it** —
    # directors before writers, writers before cast. That sequencing is the only thing that
    # ever says which seed grade cost precision; two grades degrading at once are
    # indistinguishable, which is the whole reason the ramp is three moves and not one.
    #
    # **Flip in the window after a reading, not the evening before.** The daily order is the
    # sweep at 07:00 UTC and the link run that measures at 09:05, so a flag set overnight
    # admits its grade two hours *before* the next reading is taken — and that reading then
    # covers both grades at once, which is the one thing the ramp exists to prevent. The
    # grade below it never gets a clean measurement, and no later run can recover one.
    sweep_admit_directors: bool = Field(default=False, alias="SWEEP_ADMIT_DIRECTORS")
    sweep_admit_writers: bool = Field(default=False, alias="SWEEP_ADMIT_WRITERS")
    sweep_admit_cast: bool = Field(default=False, alias="SWEEP_ADMIT_CAST")
    # The fourth tranche (D-50), and the only one that is not a seed grade: it admits a
    # candidate reached *only* through a non-seed credit of somebody a user follows (EF-2).
    # Off by default like the three above, and for the same reason — opening it is an env
    # change rather than a deploy — but it does not belong in their ramp order, because it
    # is not a wider cut of the same evidence. A follow is one user saying this person is worth
    # a request; the ramp exists to attribute a precision drop to a seed grade, and this flag's
    # reading is about the follow graph instead. The credit *history* half of the same decision
    # (D-49) needs no flag: it records for whoever is followed, and records nothing extra while
    # nobody follows anyone that widely.
    sweep_admit_followed: bool = Field(default=False, alias="SWEEP_ADMIT_FOLLOWED")
    # How many distinct seed people must reach an undated film before it may be admitted
    # (§4.1). One director attachment is the earliest and most valuable signal the product
    # sells; it is also exactly what a speculative TMDB entry looks like, and §4.2 left that
    # tension open for measurement rather than taste.
    #
    # **Measured 2026-08-11 (NEU-1087); 1 is no longer a placeholder.** The probe ran against
    # a snapshot taken minutes before the directors tranche opened — the pre-expansion
    # distribution, which cannot be retaken — and found 639 director-reached candidates of
    # 3,250. Raising the bar to 2 cuts that by 60% while the `Rumored` share, the only
    # available signature of a speculative entry, does not move: 19.4% -> 19.6%. Of the 384
    # films it would drop, 19.3% are `Rumored`, indistinguishable from the base rate — so 2
    # does not select against vaporware, it selects against being early, which is the signal
    # the product sells. It would also discard 113 films already `In Production` or `Post
    # Production` to remove 74 `Rumored` ones. Only at 3 does the `Rumored` share fall, on a
    # tranche of 87. Full table in spec §4.3.
    #
    # **The cast tranche does not reopen this either (NEU-1090).** The ramp was scoped
    # expecting cast to be the grade that forced the bar up, being the loosest signal; by
    # status it is the tightest of the three, so the tranche that was going to demand a
    # higher threshold is instead the argument for leaving it alone.
    #
    # Ground truth is still owed: "was it real" properly means what fraction later went
    # dormant, and nothing can go dormant until 2027 at the current N (NEU-1118). Status is
    # a proxy. If that figure ever contradicts this, it wins.
    sweep_corroboration_threshold: int = Field(
        default=1, ge=1, alias="SWEEP_CORROBORATION_THRESHOLD"
    )

    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    # Optional, deliberately unlike ANTHROPIC_API_KEY above: every deploy today is Anthropic
    # for all four stages, and requiring these would break every one of them for a capability
    # none of them uses (design §8). Boot-time validation (NEU-981) is what makes optional
    # safe — it asserts a credential exists for each *configured* provider, at startup.
    deepinfra_api_key: str | None = Field(default=None, alias="DEEPINFRA_API_KEY")
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    link_model: str = Field(default="claude-haiku-4-5", alias="LINK_MODEL")
    link_provider: Provider = Field(default="anthropic", alias="LINK_PROVIDER")
    cluster_model: str = Field(default="claude-sonnet-4-6", alias="CLUSTER_MODEL")
    cluster_provider: Provider = Field(default="anthropic", alias="CLUSTER_PROVIDER")
    link_confidence_floor: float = Field(default=0.7, alias="LINK_CONFIDENCE_FLOOR")
    link_recency_days: int = Field(default=4, alias="LINK_RECENCY_DAYS")
    # Re-derived at NEU-1001. The old 15 was chosen when a ~46k-token roster prefix was
    # cached and amortized across the batch; the retrieval path sends no prefix, so what
    # bounds the batch now is the **reply**: `_MAX_TOKENS` caps it at 2048, and a batch
    # whose reply is truncated fails to parse and takes every story in it down. At 20 the
    # worst-case reply measures 1,183 tok (58% of the ceiling) while the instruction block
    # falls to 11.4% of the request, against 14.7% at 15. A batch of 40 overruns the reply
    # ceiling outright.
    link_batch_size: int = Field(default=20, alias="LINK_BATCH_SIZE")
    # 8192, not 4096, since NEU-1360: the cluster reply now carries the per-story mention
    # tuples (a verbatim `evidence_span` each) in the same JSON object as the groups. A reply
    # cut off at the ceiling is not a partial loss — `parse_cluster_groups` returns None and
    # the whole film's clustering raises — so the headroom is what keeps a busy film from
    # failing on a beat it used to cluster fine.
    link_cluster_max_tokens: int = Field(default=8192, alias="LINK_CLUSTER_MAX_TOKENS")
    link_cluster_attach_limit: int = Field(default=25, alias="LINK_CLUSTER_ATTACH_LIMIT")
    link_singular_dedup_days: int = Field(default=14, alias="LINK_SINGULAR_DEDUP_DAYS")
    link_release_change_window_days: int = Field(
        default=14, alias="LINK_RELEASE_CHANGE_WINDOW_DAYS"
    )
    # T and K for `link.retrieval.select`, re-derived at NEU-1135 over the post-writers-
    # tranche catalog (2,695 active films) — see that module's docstring, and the
    # candidate-retrieval design spec §5.14 for the tuning record (§5.13 is NEU-1088's). The
    # *project* spec §7.2 is a different document; it is what made this a ticket rather than
    # a follow-on. They stay settings so the next catalog expansion is answered by config
    # than by a deploy, which is expected: the cast tranche (NEU-1090) roughly doubles the
    # catalog again. They mirror the selector's own module defaults; `config` cannot import
    # those (retrieval/index.py reads settings, so it would be a cycle), so a test pins the
    # two together instead.
    #
    # **Only K is really settable.** T looks like the other half of the pair and is not: the
    # grid finds no usable value above 0.5 at all — the next step up, whatever its spelling,
    # takes zero-candidate from 0.2% to 18.4% and breaches the hard ceiling below. Raising
    # this one from env is an incident action, not a tuning action.
    link_retrieval_threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, alias="LINK_RETRIEVAL_THRESHOLD"
    )
    link_retrieval_max_candidates: int = Field(
        default=47, ge=1, alias="LINK_RETRIEVAL_MAX_CANDIDATES"
    )
    # The hard-breach guard (NEU-1002, ADR-0010): a zero-candidate rate above the ceiling
    # finalizes the run `failed`, which aborts the daily chain and pings the deadman. Mirrors
    # `link.retrieval.health`'s own constants — same duplication, same pinning test, same
    # reason — and both stay settings so an incident is answered from env: 1.0 disarms the
    # guard, and the minimum denominator is what stops a quiet news day tripping it.
    #
    # Tightened 0.25 → 0.10 at NEU-1088. The ceiling has to stay *below* what a mis-set T
    # would produce or it cannot catch the failure ADR-0010 names, and the gap had gone
    # slack: zero-candidate falls as the catalog grows, so T=0.6's rate fell from 32.6% to
    # 25.6% and left 0.25 clearing it by 0.6pp (spec §5.13). **Left at 0.10 at NEU-1135**,
    # where T=0.6 measures 18.4% — still caught, but the margin is down to 8.4pp from 15.6pp.
    # The decay has not stopped; the next pass re-checks it rather than assuming it holds.
    link_retrieval_max_zero_candidate_rate: float = Field(
        default=0.10, ge=0.0, le=1.0, alias="LINK_RETRIEVAL_MAX_ZERO_CANDIDATE_RATE"
    )
    link_retrieval_health_min_stories: int = Field(
        default=50, ge=0, alias="LINK_RETRIEVAL_HEALTH_MIN_STORIES"
    )
    # The soft tier (NEU-1088 §3.6): a cap-saturation rate above this flags the health row
    # and names itself in the run's detail line. It does **not** fail the run — saturation is
    # drift, and `run_daily` is fail-fast. **Reaffirmed rather than re-derived at NEU-1135**:
    # on the post-writers catalog the floor is 1.89% at the new K=47, against 6.83% at the old
    # K=35 over the same corpus, so 5% keeps the ~2.6x margin it was designed with. Still
    # provisional — reaffirming is not promotion to settled, and the cast tranche is the first
    # expansion this value will meet that it was calibrated before.
    link_retrieval_saturation_warn_rate: float = Field(
        default=0.05, ge=0.0, le=1.0, alias="LINK_RETRIEVAL_SATURATION_WARN_RATE"
    )
    # Person resolution (D-21, M4). Thresholds rather than constants because M4 ships before
    # there is a corpus of resolved mentions to tune them against, and the first weeks of
    # `/admin/resolution` are what will say whether the band is too wide or too narrow —
    # an answer that should reach production from env rather than from a deploy.
    #
    # They mirror `link.resolve.scoring`'s own module defaults, which carry the derivation;
    # `config` cannot import them (that module reads none of this, but the pair is pinned by
    # a test either way, the same arrangement the retrieval settings use).
    resolve_accept_floor: float = Field(default=0.5, ge=0.0, le=1.0, alias="RESOLVE_ACCEPT_FLOOR")
    resolve_accept_margin: float = Field(
        default=0.12, ge=0.0, le=1.0, alias="RESOLVE_ACCEPT_MARGIN"
    )
    # One `/search/person` request per uncached mention, so this is a request budget as much
    # as a work limit: the first run after deploy faces every mention clustering has ever
    # extracted. The remainder is the next run's backlog, not a loss.
    resolve_mentions_per_run: int = Field(default=500, ge=0, alias="RESOLVE_MENTIONS_PER_RUN")
    # The kill switch. Resolution is the one part of the link stage that talks to TMDB, so an
    # outage there is answered by turning it off for a day rather than by letting the pass
    # burn its budget on retries — the mentions keep until it is back on.
    resolve_enabled: bool = Field(default=True, alias="RESOLVE_ENABLED")
    # The `resolve` gateway stage (D-22): the closed-set tiebreak the scoring pass routes its
    # narrow band to. Sonnet rather than the Haiku the other three stages default to, on
    # volume *and* on difficulty: D-22 targets ≤10% of mentions, and the band is by
    # construction the case deterministic features could not separate — two people TMDB knows
    # by the same name. That is the wrong-Chris-Evans failure M4 exists to make impossible,
    # so it is the one stage where the cheaper model is the false economy.
    resolve_model: str = Field(default="claude-sonnet-4-6", alias="RESOLVE_MODEL")
    resolve_provider: Provider = Field(default="anthropic", alias="RESOLVE_PROVIDER")
    source_gate_enabled: bool = Field(default=True, alias="SOURCE_GATE_ENABLED")
    source_judge_model: str = Field(default="claude-haiku-4-5", alias="SOURCE_JUDGE_MODEL")
    source_judge_provider: Provider = Field(default="anthropic", alias="SOURCE_JUDGE_PROVIDER")
    source_unresolved_tier: str = Field(default="acceptable", alias="SOURCE_UNRESOLVED_TIER")
    summary_model: str = Field(default="claude-haiku-4-5", alias="SUMMARY_MODEL")
    # Named for the setting beside it, not for the stage: the stage is `summarize`, and
    # `Gateway` owns that one-line mapping rather than renaming a live env var.
    summary_provider: Provider = Field(default="anthropic", alias="SUMMARY_PROVIDER")
    summary_prompt_version: str = Field(default="9", alias="SUMMARY_PROMPT_VERSION")
    url_resolve_per_run: int = Field(default=500, alias="URL_RESOLVE_PER_RUN")
    url_resolve_max_attempts: int = Field(default=3, alias="URL_RESOLVE_MAX_ATTEMPTS")
    url_resolve_delay_seconds: float = Field(default=1.0, alias="URL_RESOLVE_DELAY_SECONDS")
    feed_recency_days: int = Field(default=3, alias="FEED_RECENCY_DAYS")
    # NEU-717 master gate: when off, no Google News at all (broad queries + per-film),
    # regardless of feeds_per_film_enabled. Paused by default on a trial basis.
    news_google_enabled: bool = Field(default=False, alias="NEWS_GOOGLE_ENABLED")
    feeds_per_film_enabled: bool = Field(default=True, alias="FEEDS_PER_FILM_ENABLED")
    feeds_per_film_throttle_seconds: float = Field(
        default=1.0, alias="FEEDS_PER_FILM_THROTTLE_SECONDS"
    )
    per_film_title_filter_enabled: bool = Field(default=True, alias="PER_FILM_TITLE_FILTER_ENABLED")
    per_film_title_match_min_ratio: float = Field(
        default=0.4, alias="PER_FILM_TITLE_MATCH_MIN_RATIO"
    )

    ingest_consecutive_failure_threshold: int = Field(
        default=10, alias="INGEST_CONSECUTIVE_FAILURE_THRESHOLD"
    )
    # How long a `running` run may go without a heartbeat before startup/scheduled-task
    # cleanup cancels it. Read against `last_progress_at`, not `started_at` (NEU-1117), so
    # this bounds *silence*, not total runtime: ~5x the longest legitimate gap anywhere
    # (one retrying LLM batch) and 30x the sweep's heartbeat interval. It was 15 when it
    # meant runtime, which cancelled live multi-hour sweeps on any restart.
    ingest_stale_run_minutes: int = Field(default=30, alias="INGEST_STALE_RUN_MINUTES")

    # healthchecks.io deadman ping URLs for the Coolify scheduled tasks (see
    # upmovies.pipeline_run). Optional: unset → the ping is a no-op, so local/dev runs of
    # `python -m upmovies.pipeline_run` don't need them. `daily` runs the full chain
    # (tmdb → feeds → link → synthesize); `hourly` runs the light feeds-only pass; `sweep`
    # runs the undated-film pass on its own slot ~2h ahead of daily, with its own deadman
    # because a sweep that stops running is invisible in the daily chain's ping (§6.1).
    healthcheck_daily_url: str | None = Field(default=None, alias="HEALTHCHECK_DAILY_URL")
    healthcheck_hourly_url: str | None = Field(default=None, alias="HEALTHCHECK_HOURLY_URL")
    healthcheck_sweep_url: str | None = Field(default=None, alias="HEALTHCHECK_SWEEP_URL")
    # `providers` is the D-27 poll, on its own slot and so with its own deadman for the same
    # reason the sweep has one: it is not a stage in the daily chain, so a poll that stopped
    # running would leave every other check green while the home-release beat went silent.
    healthcheck_providers_url: str | None = Field(default=None, alias="HEALTHCHECK_PROVIDERS_URL")
    # `notify` is the M7 decision pass (D-31), scheduled after the daily chain rather than
    # inside it: the chain publishes the events, and a decision pass that ran as a fifth stage
    # would abort with it and mail nobody about the four stages that did succeed. Its own
    # deadman for the same reason as the two above — and a sharper one, because this pass
    # failing is silence for the *user*, not just for the catalogue.
    healthcheck_notify_url: str | None = Field(default=None, alias="HEALTHCHECK_NOTIFY_URL")
    # `digest daily` and `digest weekly` are the M7 digest sender (D-33) on two slots — one per
    # cadence, because a healthchecks.io check has one schedule and a daily check cannot also
    # expect a weekly ping. Each carries its own deadman for the notify slot's reason: the
    # failure this reports is silence for the user.
    healthcheck_digest_daily_url: str | None = Field(
        default=None, alias="HEALTHCHECK_DIGEST_DAILY_URL"
    )
    healthcheck_digest_weekly_url: str | None = Field(
        default=None, alias="HEALTHCHECK_DIGEST_WEEKLY_URL"
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    cors_allowed_origins_raw: str = Field(
        default="https://app.upmovies.localhost", alias="CORS_ALLOWED_ORIGINS"
    )
    public_base_url: str = Field(default="http://localhost:5173", alias="PUBLIC_BASE_URL")
    # Where TMDB's image CDN serves poster paths from, without a size segment. The frontend
    # builds its own URLs from `VITE_TMDB_IMAGE_BASE` and this is the same value; the backend
    # needs its own because a *mail* carries absolute image URLs — there is no page around the
    # `<img>` to resolve a relative path against, and no JS to build one at render time.
    tmdb_image_base: str = Field(default="https://image.tmdb.org/t/p", alias="TMDB_IMAGE_BASE")

    session_cookie_name: str = Field(default="upmovies_session", alias="SESSION_COOKIE_NAME")
    csrf_cookie_name: str = Field(default="csrf_token", alias="CSRF_COOKIE_NAME")
    session_ttl_days: int = Field(default=30, alias="SESSION_TTL_DAYS")
    cookie_secure: bool = Field(default=True, alias="COOKIE_SECURE")
    cookie_samesite: str = Field(default="lax", alias="COOKIE_SAMESITE")
    cookie_domain: str | None = Field(default=None, alias="COOKIE_DOMAIN")

    login_lockout_threshold: int = Field(default=5, alias="LOGIN_LOCKOUT_THRESHOLD")
    login_lockout_window_minutes: int = Field(default=15, alias="LOGIN_LOCKOUT_WINDOW_MINUTES")

    # Per-IP token buckets for the anonymous surface (D-19, NEU-1344). Each is the raw
    # `"<capacity>/<refill_per_minute>"` string the bucket is built from; `app.rate_limit`
    # parses it and a boot-time check refuses a malformed one, in the manner of every other
    # config fault here. The numbers are *not* duplicated in that module — unlike the
    # retrieval constants above, which mirror their module's defaults — because a limit with
    # no configured value is a route with no limit, and there is nothing sensible to fall back
    # to. `capacity` is the burst a single address may spend at once; `refill_per_minute` is
    # the sustained rate underneath it, so `"5/0.083"` reads as "five now, five an hour".
    #
    # Two buckets are registered ahead of the routes that will use them — `import` is M3 and
    # `ics` is M7 — because of the Coolify gotcha in `AGENTS.md`: a variable absent from the
    # deployment at first deploy is one somebody has to add by hand later.
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    # The public bucket ships wired but inert, and it is the only one that does. Until the
    # Worker signs its requests (NEU-1389) every server-rendered page arrives from a handful
    # of Cloudflare egress IPs, so a live `public` bucket would throttle the whole anonymous
    # site through them. Rollout order is spec §5: deploy this, deploy the Worker, then flip
    # this flag in the Coolify UI.
    rate_limit_public_enabled: bool = Field(default=False, alias="RATE_LIMIT_PUBLIC_ENABLED")
    rate_limit_signup: str = Field(default="5/0.083", alias="RATE_LIMIT_SIGNUP")
    # Wider than the rest, and on top of the per-email lockout above rather than instead of
    # it: the two answer different questions — that one counts failures against one address,
    # this one counts requests from one host — and a household or an office behind one NAT is
    # a legitimate source of a good many logins.
    rate_limit_login: str = Field(default="20/1.33", alias="RATE_LIMIT_LOGIN")
    # Password reset, verification re-send, email-change request: the three routes that put a
    # message in somebody else's inbox, so the limit is really a bound on how much mail an
    # anonymous caller can make this service send.
    rate_limit_auth_request: str = Field(default="5/0.083", alias="RATE_LIMIT_AUTH_REQUEST")
    rate_limit_import: str = Field(default="6/0.1", alias="RATE_LIMIT_IMPORT")
    rate_limit_public: str = Field(default="240/120", alias="RATE_LIMIT_PUBLIC")
    rate_limit_ics: str = Field(default="30/30", alias="RATE_LIMIT_ICS")
    # The secret the SSR Worker signs its API calls with (`X-Backlotter-Origin`), which is what
    # lets it name the visitor it is rendering for instead of being metered as one caller.
    # `None` by default — and an unset secret means the header is ignored entirely, never that
    # a signed request is refused, so this deploying before the Worker sibling costs nothing.
    ssr_origin_secret: str | None = Field(default=None, alias="SSR_ORIGIN_SECRET")

    # Cloudflare Turnstile's server-side secret (D-18, M1) — the whole of what stops `POST
    # /auth/signup` being a script's endpoint now that an invite code is no longer required.
    #
    # Empty by default, and empty means signup is **refused** rather than unguarded. That is
    # the opposite of the call `MAIL_PROVIDER` makes above, deliberately: a forgotten mail
    # provider sends nothing, which is recoverable and loud in the log, while a forgotten
    # secret that failed open would take real signups from real people the whole time it went
    # unnoticed — they look exactly like the legitimate ones, and there is no undo. A deploy
    # that has not set the Coolify variable yet (spec §7) refuses signups for those minutes;
    # `app/turnstile.py` argues the direction, `deps.get_turnstile` serves the 503.
    #
    # `dev-bypass` is the one value that verifies without calling Cloudflare, for local work
    # and the suite, which have no site key and are forbidden the live network anyway.
    turnstile_secret: str = Field(default="", alias="TURNSTILE_SECRET")
    # The rollback switch for open signup (D-18). False does not close signup — it restores
    # the invite requirement, which is what "rolling back" this change means: the state the
    # route was in before, with the comped-invite path still working and Turnstile still
    # verified. Closing signup outright is not offered here, because the failure this exists
    # to answer is "the open door is being abused", and an admin who can still issue invites
    # can still let people in while it is shut.
    signup_open: bool = Field(default=True, alias="SIGNUP_OPEN")

    # Transactional mail (D-30, M1). `noop` by default, and the default is the interesting
    # part: every deploy that exists today has no Resend account, so defaulting to `resend`
    # would fail each of their boots the moment this merges, for a capability none of them
    # uses yet. The cost of the safe default is a production container that silently sends
    # nothing if `MAIL_PROVIDER` is forgotten — which is why `mail.noop` logs every send at
    # INFO naming the recipient and the provider, so "forgotten" is visible in the log stream
    # instead of only in a support ticket.
    mail_provider: MailProvider = Field(default="noop", alias="MAIL_PROVIDER")
    # The envelope sender, in either the bare `a@b.c` or the `Name <a@b.c>` form. Empty by
    # default rather than carrying an invented address: a plausible-looking default is one
    # nobody notices is wrong until mail bounces, whereas empty is refused at boot for any
    # provider that actually transmits (`mail.gateway.validate_mail_configuration`).
    mail_from: str = Field(default="", alias="MAIL_FROM")
    # Optional, deliberately unlike ADMIN_TOKEN above and for the same reason the two
    # non-Anthropic LLM keys are: requiring it would break every deploy that is not sending
    # mail. Boot-time validation is what makes optional safe — it asserts a credential exists
    # for the *configured* provider, at startup.
    resend_api_key: str | None = Field(default=None, alias="RESEND_API_KEY")
    # What mail templates call the product. A setting rather than a constant in the template
    # because every template needs it and the name is the one thing in them that is a property
    # of the deployment: a staging environment sending mail that calls itself the production
    # product is how a test send gets mistaken for a real one.
    product_name: str = Field(default="Backlotter", alias="PRODUCT_NAME")
    # How long an emailed verification link stays good (M1). A day is long enough to survive a
    # mail that lands overnight and short enough that a forwarded mail is not a standing key to
    # the account. The value is also mail *copy* — the template tells the reader how long they
    # have — so the two cannot drift: `verification_service` passes this into the context.
    verify_token_ttl_hours: int = Field(default=24, ge=1, alias="VERIFY_TOKEN_TTL_HOURS")
    # How long an emailed password-reset link stays good (M1). Deliberately much shorter than
    # the verification window above, because the two links are not worth the same: a spent
    # verification token stamps a column, a spent reset token *is* the account — it sets the
    # password and drops every session. An hour is the window someone who just asked for the
    # mail actually needs. Like the value above, this is also mail copy, so `reset_service`
    # passes it into the template context rather than letting the two drift.
    reset_token_ttl_hours: int = Field(default=1, ge=1, alias="RESET_TOKEN_TTL_HOURS")
    # How long an emailed email-change confirmation stays good (M1). An hour, matching the
    # reset window rather than the verification one, because this token is reset-grade and not
    # verify-grade: a spent verification token stamps a column, whereas spending this one moves
    # the address the account signs in and recovers with — after which whoever holds the new
    # inbox can take the password too. The flow is also synchronous by nature, so the short
    # window costs a legitimate user one more click and denies a mistyped address a standing
    # key. Mail copy as well as policy, so `email_change_service` passes it into the context.
    email_change_token_ttl_hours: int = Field(default=1, ge=1, alias="EMAIL_CHANGE_TOKEN_TTL_HOURS")

    # Web Push (D-36). The VAPID keypair identifies *this deployment* to every push service —
    # the public key is handed to the browser at subscribe time and baked into the endpoint it
    # gets back, and the private key signs each send. Changing either invalidates every
    # subscription taken out under the old pair, so these are generated once per deployment and
    # kept: rotating them is a re-subscribe for every user, not a config edit.
    #
    # All three optional and empty by default, like the mail credential above and for the same
    # reason — a deploy that is not doing push must still boot. What makes optional safe here
    # is `push.validate_push_configuration`, which refuses the boot only once a
    # `push_subscription` row exists: by then a browser is waiting for notifications that an
    # unconfigured process cannot send, and silence is the failure nobody reports.
    vapid_public_key: str = Field(default="", alias="VAPID_PUBLIC_KEY")
    vapid_private_key: str = Field(default="", alias="VAPID_PRIVATE_KEY")
    # Who to contact about this deployment's pushes, as a `mailto:` or `https:` URL. Part of
    # the VAPID claim rather than decoration: a push service with a misbehaving sender uses it
    # before it starts rejecting, and some of them refuse a claim without it outright.
    vapid_subject: str = Field(default="", alias="VAPID_SUBJECT")

    @property
    def cors_allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins_raw.split(",") if o.strip()]

    @property
    def tmdb_excluded_statuses(self) -> frozenset[str]:
        return frozenset(s.strip() for s in self.tmdb_excluded_statuses_raw.split(",") if s.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
