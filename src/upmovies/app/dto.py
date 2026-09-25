from datetime import UTC, date, datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from upmovies.catalog.headline_release import HeadlineRelease, HeadlineReleaseKind


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    # No control characters, newlines included (NEU-1341). A display name is one line of text
    # by definition, and this is the only route that sets one — but it is also a value that
    # every transactional mail interpolates into its *plain-text* part, where autoescape is
    # deliberately off (`mail/templates.py`) because escaping a text body renders `&amp;` to
    # the reader. `mail.templates.render` already collapses the subject for this reason; the
    # body cannot be collapsed without destroying the template's own layout, so the value is
    # constrained where it enters instead. Until NEU-1341 the blast radius was self-inflicted
    # — every mail went to the account's own address — and `POST /auth/email-change/request`
    # is the first route that mails an address the caller merely names.
    display_name: str = Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")
    # The solved Turnstile challenge, verified server-side before anything is written
    # (NEU-1343, D-18). Required, and required whatever `SIGNUP_OPEN` says: the invite code
    # was never a bot check — it was a *scarcity* check, and it stopped being either the
    # moment signup opened. 2048 is the ceiling Cloudflare documents for the response value;
    # the bound is here so a megabyte of body is refused before it reaches an outbound call.
    turnstile_token: str = Field(min_length=1, max_length=2048)
    # Optional since NEU-1343: an invite is the admin comp path, not the gate. Supplied, it
    # is still validated and consumed exactly as before — a code that is unknown, spent or
    # issued to another address fails the signup rather than being ignored, because someone
    # typing a code in is telling us they were given one, and silently opening a plain
    # account instead would hide a mistake worth seeing. Required again when `SIGNUP_OPEN`
    # is off, which is that setting's whole job.
    invite_code: str | None = Field(default=None, min_length=1, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class AccountDeleteRequest(BaseModel):
    password: str


class VerificationRequest(BaseModel):
    """The address to (re-)send a verification mail to. Unauthenticated: the mail may be opened
    on a device that was never signed in, and the reset flow that follows in M1 has the same
    shape."""

    email: EmailStr


class VerificationConsumeRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class PasswordResetRequest(BaseModel):
    """The address to mail a reset link to. Unauthenticated, and answered identically whether
    or not there is an account behind it."""

    email: EmailStr


class PasswordResetConsumeRequest(BaseModel):
    """A reset link plus the password to set with it.

    `new_password` carries the same bounds as `SignupRequest.password` and
    `PasswordChangeRequest.new_password`: a reset is one of three ways a password gets set, and
    the weakest of them would be the one that decides the policy."""

    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=128)


class EmailChangeRequest(BaseModel):
    """The address to move the account to, plus the password that authorises the move.

    The password is asked for even though the route is already authed and CSRF-guarded, because
    a session left open on a borrowed machine is exactly the way an account gets walked off
    with — and an address change is the one edit that takes the account with it."""

    new_email: EmailStr
    current_password: str


class EmailChangeConfirmRequest(BaseModel):
    """The token from the mail sent to the new address. Nothing else: the address to move to
    was fixed when the token was issued, so accepting it again here would let the holder of a
    link redirect the change somewhere the account owner never nominated."""

    token: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str
    is_admin: bool
    # Derived from `email_verified_at` rather than exposing the timestamp: the frontend's
    # AuthContext asks a yes/no question (M1 contract), and the date is not the client's
    # business. Every authed response carries it, not just `GET /me`, so the context is
    # populated from the signup and login replies too.
    email_verified: bool
    # Derived from `entitled_until` for the same reason and on the same terms (D-41): the
    # frontend's AuthContext branches on a yes/no — locked panel or timeline — and the grant's
    # end date is the admin surface's business, not the account holder's. Every authed response
    # carries it, so a grant made while the user is signed in takes effect on their next
    # `GET /me` rather than needing a sign-out.
    entitled: bool
    created_at: datetime


class AuthedUserOut(UserOut):
    csrf_token: str


class InviteCreateRequest(BaseModel):
    email_hint: EmailStr | None = None


class InviteOut(BaseModel):
    """One invite as the admin page sees it (NEU-1408).

    `consumed_by_email` is resolved at read time from the consuming account, because an admin
    looking at a spent code wants to know *who* spent it, and `/admin/users` searches by address,
    not by id. Both consumer fields are null for an outstanding code, and also for one spent by
    an account that has since been deleted (the FK is `ON DELETE SET NULL`)."""

    code: str
    email_hint: str | None
    created_at: datetime
    consumed_at: datetime | None
    consumed_by_user_id: UUID | None
    consumed_by_email: str | None


class AdminUserOut(BaseModel):
    """One account as the admin grant page sees it (D-38).

    Distinct from `UserOut`, which is the account holder's view of themselves: this one carries
    the raw `entitled_until` and `email_verified_at` timestamps rather than the booleans derived
    from them, because an admin deciding whether to extend a grant is asking *when*, not
    *whether*. `display_name` and anything resembling a credential are deliberately absent —
    the page lists accounts to grant access to, it is not an account inspector."""

    id: UUID
    email: str
    is_admin: bool
    email_verified_at: datetime | None
    entitled_until: datetime | None
    created_at: datetime


class AdminUserPage(BaseModel):
    """A page of accounts plus the total it was drawn from, so the caller can page a search
    whose result count it cannot otherwise know."""

    items: list[AdminUserOut]
    total: int
    limit: int
    offset: int


class EntitlementGrantRequest(BaseModel):
    """The new expiry for a grant. A naive value is read as UTC rather than refused, the way
    `/admin/runs`'s `since` is: an admin page posting a date picker's value should get the day
    it meant, not one shifted by the server's timezone."""

    entitled_until: datetime

    @field_validator("entitled_until")
    @classmethod
    def _assume_utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v


class DigestTestRequest(BaseModel):
    """Whose digest to mail the calling admin, on which cadence, as of which day (DC-11).
    `today` defaults to the current UTC date, as the preview's does."""

    user_id: UUID
    cadence: Literal["daily", "weekly"]
    today: date | None = None


class DigestTestOut(BaseModel):
    message_id: str


class HeadlineReleaseOut(BaseModel):
    """The one date a film row leads with, and enough context to render it honestly.

    `kind` is the difference between a date this site lists and TMDB's primary date, which it
    does not (`catalog.headline_release`): `upcoming` and `released` come from a displayable
    subject and carry that subject's `country` and `bucket`, while `primary` is the last-resort
    fallback and carries neither, so a client can mark it unconfirmed. The bucket identifiers
    are lowercase `limited`/`wide` — display labels are the frontend's business."""

    date: date
    kind: HeadlineReleaseKind
    country: str | None
    bucket: str | None


def headline_release_out(headline: HeadlineRelease | None) -> HeadlineReleaseOut | None:
    """The catalog's answer as the wire field, passing `None` through.

    Here rather than in one of the two readers — the entity pages' film rows and the follows
    page's title rows — because both spell the same four fields and a second copy is how one of
    them would come to drop `bucket`."""
    return (
        None
        if headline is None
        else HeadlineReleaseOut(
            date=headline.date,
            kind=headline.kind,
            country=headline.country,
            bucket=headline.bucket,
        )
    )


# --- follows (M3, D-10, EF-1) ------------------------------------------------------------------

FollowEntityType = Literal["person", "company", "franchise", "title"]
AlertStore = Literal["buy", "rent", "stream"]


def normalise_entity_id(entity_type: str, entity_id: str) -> str:
    """The canonical spelling of an entity id, so one entity cannot be followed twice under two
    spellings of the same id (`"12"` / `"012"`, a UUID in either case).

    Person, company and franchise ids are TMDB integers; a title is a `catalog.film` row, whose id
    is our UUID. Raises `ValueError` — which pydantic turns into a 422 — for a value that is not an
    id of the right shape for its type."""
    if entity_type == "title":
        return str(UUID(entity_id))
    value = int(entity_id)
    if value <= 0:
        raise ValueError("entity_id must be a positive integer")
    return str(value)


class FollowCreateRequest(BaseModel):
    """A new follow: the entity, and nothing else (EF-1).

    A `coverage` in the body is **ignored**, not refused, and that is a deliberate two-deploy
    kindness: M2 drops the tier from the backend while the M3 frontend still draws the control
    (EF-1, spec §6), so 422-ing the field would break every follow button in the live client
    for the length of the gap. Pydantic's default `extra="ignore"` is what does it: the field
    is simply not declared here, so an unknown key is dropped on the way in."""

    entity_type: FollowEntityType
    entity_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _normalise(self) -> "FollowCreateRequest":
        self.entity_id = normalise_entity_id(self.entity_type, self.entity_id)
        return self


class FollowOut(BaseModel):
    """A follow, with enough of the entity to label the row.

    `name` and `image_path` are nullable and that is load-bearing: a follow can outlive the
    entity it names (a person purged from TMDB, a row written before a backfill), and D-40 says
    nothing here deletes user graph rows. An unresolvable follow is listed with nulls rather
    than filtered out.

    `last_activity_at` is the `created_at` of the newest visible card this follow delivers
    (EF-15) — every beat on the film for a title row, the entity's own attach, detach and
    `canceled` cards for the other three (EF-3) — and it is the follows page's third sort.
    **Null means nothing has happened yet**, not "unknown": a follow taken out this morning on
    a film the site has never carded is a real and common state, and the page reads it as the
    bottom of that sort rather than as a missing value.

    `headline_release` is **title rows only** and null on every other type (EF-14). A followed
    film has one date worth leading with and the follows page shows it; a followed person does
    not have a date of their own, and inventing one — the next release they are credited on,
    say — would be the indirect reach this project has just taken away, smuggled back in as a
    column. It is the same field `public.dto.FilmRowOut` carries, filled from the same batch
    query (`catalog.headline_release`), so a film's date on the follows page and on an entity
    page cannot disagree. Null for a title row too when the film has no displayable release row
    and no primary date."""

    entity_type: str
    entity_id: str
    name: str | None
    image_path: str | None
    headline_release: HeadlineReleaseOut | None
    source: str
    created_at: datetime
    last_activity_at: datetime | None


class FollowListResponse(BaseModel):
    items: list[FollowOut]


def normalise_alert_stores(stores: list[str]) -> list[str]:
    """Canonical order, no duplicates, so two store lists that mean the same thing compare
    equal and the row reads the same however the client spelled it."""
    return [s for s in ("buy", "rent", "stream") if s in stores]


# The imports (D-15, D-16). The job row as the owner polls it — every field of `app.import_job`
# except `user_id`, which the caller is.


_MATCHED_KINDS = ("outside_window",)
"""`kind` values in `app.import_job.unmatched` that name a film the import *did* match — written
only by jobs that ran between NEU-1448 and EF-22 (NEU-1449), which moved those rows onto
`app.import_candidate`. Dropped on the way out: a matched film listed under "titles we could
not match" would be false, and would invite the very follow EF-21 declined."""


class ImportUnmatchedOut(BaseModel):
    """A title the import could not place, and why.

    `name` and `year` are the source's, verbatim, because the user is going to look for them in
    their own export or their own TMDB list. The three kinds are two different failures: a
    Letterboxd row is unmatched because no `/search/movie` rule would place the title it names
    (`watchlist`, `rating`), while a TMDB row is unmatched only when TMDB has since deleted the
    entry its own list still points at (`tmdb_missing`) — there is no resolution step in that
    import to fail, because the ids are authoritative (D-16).

    `rating` is **historical**: the ratings path is deleted (EF-20) and nothing writes it any
    more, but `app.import_job.unmatched` is a JSONB column and jobs that ran before M5 still
    hold rows carrying it. Polling one of those must not 500 on its own report.

    A film the alert window closed on is **not** here — it was matched, and is in the catalog.
    It is an `ImportCandidateOut` with a `skip_reason`."""

    name: str
    year: int | None = None
    kind: Literal["watchlist", "rating", "tmdb_missing"]


class ImportCandidateOut(BaseModel):
    """One film on an import's review list (EF-22), as `app.import_candidate` holds it.

    `selected` is the tick the list opens with. A row with a `skip_reason` is unticked and not
    selectable — the confirm ignores its id — and is listed so the user can see what the import
    declined (EF-21). `title` is the catalog's, so a wrong Letterboxd match is visible before it
    becomes a follow; `headline_release` is the date the row leads with, as on the follows
    page, snapshotted when the job ran."""

    model_config = ConfigDict(from_attributes=True)

    film_id: UUID
    tmdb_id: int
    title: str
    headline_release: HeadlineReleaseOut | None
    selected: bool
    skip_reason: Literal["outside_window"] | None


class ImportConfirmIn(BaseModel):
    """The films the user kept from the review list (EF-22). Ids that are not selectable
    candidates of the job are ignored rather than refused, so a stale list costs the user
    nothing but the rows that went stale.

    Capped at the size of the largest list an import can propose — both runners read at most
    5,000 rows — so a body cannot ask the confirm to look up more ids than a job could hold."""

    film_ids: list[UUID] = Field(max_length=5_000)


class TMDBCallbackIn(BaseModel):
    """What the frontend forwards after themoviedb.org sends the user back (D-16).

    TMDB appends `request_token` and `approved` to `TMDB_REDIRECT_URL`; the page reads both and
    posts them here. `approved` is carried rather than assumed because TMDB sends the user back
    either way, and a refusal must not be answered by trying to exchange the token."""

    request_token: str = Field(min_length=1, max_length=256)
    approved: bool


class ImportJobStartedOut(BaseModel):
    """The 202 from an upload: the id to poll, and nothing else — the job has not run yet."""

    job_id: UUID


class ImportJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: str
    status: str
    rows_total: int
    rows_done: int
    # `watchlist_created` is the films the job offered — the ticked rows of its review list —
    # and `follows_created` the rows the user confirmed (EF-22): zero until the confirm, and
    # not every film offered, because the user can untick some.
    watchlist_created: int
    follows_created: int
    # Titles the import could not place. Films it placed are `candidates`, ticked or not.
    unmatched: list[ImportUnmatchedOut]
    # The review list, only while `status` is `awaiting_review` (EF-22) and empty otherwise:
    # the rows are deleted once the list is confirmed or superseded, when the follows — or the
    # next import — are the record. Filled by the route, not read off the job row.
    candidates: list[ImportCandidateOut] = Field(default_factory=list)

    @field_validator("unmatched", mode="before")
    @classmethod
    def _failures_only(cls, rows: object) -> object:
        if not isinstance(rows, list):
            return rows
        return [r for r in rows if not (isinstance(r, dict) and r.get("kind") in _MATCHED_KINDS)]

    # The TMDB account a `tmdb` job read, for "Imported from @user"; NULL on a Letterboxd job
    # and the only thing kept about that account (D-16).
    tmdb_username: str | None = None
    # Set only on a `failed` job. The runner writes `str(exception)` here, which is why the
    # route is the one place it is rendered: it is a one-line cause for a user to quote back,
    # not a payload anything should branch on — with one exception, `superseded`
    # (`app.models.IMPORT_SUPERSEDED`), which is ours: the list was discarded because the user
    # started another import before confirming it (EF-22).
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


# --- delivery settings (M7, D-33, D-34) -------------------------------------------------------

DigestCadence = Literal["daily", "weekly", "off"]


class UserSettingsOut(BaseModel):
    """The whole of `/me/settings`, which is also the whole of `app.user_settings`.

    `ical_token` is returned rather than only the rendered URL: the calendar panel has to show
    the URL, offer it as a `webcal:` link and let the user copy it, and the base it hangs off is
    the frontend's own knowledge of the API origin. Returning the token to its owner is not a
    disclosure — it is theirs, over an authenticated, entitlement-gated request (D-39)."""

    model_config = ConfigDict(from_attributes=True)

    digest_cadence: DigestCadence
    alert_stores: list[str]
    """Which availability beats this user is alerted on, product-wide (D-44). `[]` is a real
    answer — no store alerts — and is not the same as the default `["stream"]`."""
    ical_token: str
    created_at: datetime
    updated_at: datetime


class UserSettingsUpdateRequest(BaseModel):
    """A PATCH of the settings: either field, or both.

    Both are optional and at least one is required, which is the shape a two-field PATCH wants
    — the settings screen writes the control the user touched, not the whole row, and a
    required `digest_cadence` would make changing the stores restate the cadence. An empty body
    stays a `422`: it is a client bug, and pydantic saying so is cheaper than a route that
    quietly does nothing. D-36's push preferences land beside them on the same terms."""

    digest_cadence: DigestCadence | None = None
    alert_stores: list[AlertStore] | None = None

    @model_validator(mode="after")
    def _normalise(self) -> "UserSettingsUpdateRequest":
        if self.digest_cadence is None and self.alert_stores is None:
            raise ValueError("no_settings_given")
        if self.alert_stores is not None:
            self.alert_stores = cast(
                list[AlertStore], normalise_alert_stores(list(self.alert_stores))
            )
        return self


# --- web push (M7, D-36) ----------------------------------------------------------------------


class PushSubscriptionKeys(BaseModel):
    """The encryption material the browser generated for one subscription.

    Nested rather than flattened because this is the shape `PushSubscription.toJSON()` produces
    in the browser: the client posts what the Push API handed it, unmodified, and a route that
    demanded a re-shaped body would be asking every caller to do the same rearranging."""

    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=256)


class PushSubscribeRequest(BaseModel):
    """`POST /me/push` — one browser registering for notifications (D-36).

    `expirationTime`, the third member of the browser's JSON, is deliberately not modelled and
    not stored: it is null in every current implementation, and a column nothing writes is a
    field the sender would eventually be tempted to trust."""

    # Bounded, because it is stored: a push endpoint is a URL the *service* mints, around 200
    # characters today, and nothing legitimate approaches this ceiling.
    endpoint: str = Field(min_length=1, max_length=2048)
    keys: PushSubscriptionKeys


class PushUnsubscribeRequest(BaseModel):
    """`DELETE /me/push` — the endpoint the browser has just torn down.

    A body rather than a query string, for the same reason the subscribe route takes one: the
    endpoint is the browser's, it is long, and it has no business in a URL an access log keeps.
    """

    endpoint: str = Field(min_length=1, max_length=2048)


class VapidPublicKeyOut(BaseModel):
    """`GET /me/push/vapid-public-key` — what the browser passes to
    `pushManager.subscribe({applicationServerKey})` (D-36).

    Served rather than built into the frontend bundle because it is a property of the
    deployment: staging and production hold different keypairs, and a bundle carrying one of
    them would subscribe every staging browser to production's endpoints."""

    public_key: str
