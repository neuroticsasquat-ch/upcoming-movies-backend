from datetime import datetime
from uuid import UUID

from sqlalchemy import (  # noqa: I001
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from upmovies.db import Base


class User(Base):
    __tablename__ = "user"
    __table_args__ = {"schema": "app"}

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # When this address was confirmed, NULL until it is (M1 contract). A timestamp rather than
    # a boolean because "when" is the question the later milestones actually ask — D-31's
    # notify pass suppresses mail to an unverified user, and a support ticket about a mail
    # that did not arrive is answered by the date, not by `false`.
    #
    # Deliberately gates nothing but outbound mail (D-18): an unverified user keeps full app
    # access, and `app.verification.is_verified` is the single place that reads this column so
    # the milestones that do gate on it do not each re-derive the rule.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When this account's access to subscriber functionality runs out; NULL means it never
    # had any (D-37). Closed by default and closed for every existing row — there is no
    # backfill, no signup trial and deliberately no global "everyone is entitled" setting,
    # because the only way in is a per-user grant (D-38) until *bl: Subscription & Billing*
    # takes over writing this column from a payment provider.
    #
    # A timestamp rather than a boolean so that ending a grant is a write of a past value
    # rather than a delete: revoking suppresses and never destroys (D-40), and the date is
    # what answers "when did this lapse?" for a support ticket. `app.entitlements` is the
    # single place that turns it into a yes/no, so the M3 and M7 surfaces that gate on it do
    # not each re-derive the rule.
    entitled_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Session(Base):
    __tablename__ = "session"
    __table_args__ = (
        Index("ix_session_user_id", "user_id"),
        Index("ix_session_expires_at", "expires_at"),
        {"schema": "app"},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app.user.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempt"
    __table_args__ = (
        Index("ix_login_attempt_email_at", "email", "attempted_at"),
        {"schema": "app"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)


class Invite(Base):
    __tablename__ = "invite"
    __table_args__ = {"schema": "app"}

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    email_hint: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app.user.id", ondelete="SET NULL"), nullable=True
    )


class EmailToken(Base):
    """A single-use, expiring token mailed to an address to prove the recipient holds it.

    One table with a `purpose` column rather than one table per flow, because M1 asks for
    three flows that differ only in what consuming the token *does* — verification here,
    password reset (NEU-1340) and email change (NEU-1341) — and the row shape (who, what for,
    issued, expires, consumed) is the same for all three. `purpose` is what stops a token
    issued for one from being spent on another.

    The token string is the primary key and is stored as issued, the way `app.session.id` and
    `app.invite.code` already are: a reader of this table holding a live verification token
    can already read `app.session`, which is the stronger credential of the two. Single use is
    `consumed_at`, not a delete, so a second click on the same link is distinguishable from a
    link that never existed.

    One qualification on that last point, since NEU-1340: a completed password reset stamps
    `consumed_at` on the user's *other* live reset tokens as well, to retire them. So the
    column means "no longer spendable, and here is when it stopped being so" rather than
    strictly "someone clicked this"; for `purpose='reset'` rows the two are not the same
    question. Nothing reads it to answer either one — `InvalidToken` is deliberately one error
    for every cause — so this costs forensic precision and no behaviour.

    `new_email` is the one column that is not shared: it carries the address an email-change
    token moves the account *to*, and is NULL for the other two purposes. A nullable column on
    the shared table rather than a table of its own, because the flow is the third of the three
    this table was built for and differs from its siblings only in what consuming the token
    does. It is deliberately not a CHECK constraint keyed on `purpose` — that would spell a
    purpose string into DDL, where changing it costs a migration — so `email_change_service` is
    the one writer that sets it and the one reader that requires it."""

    __tablename__ = "email_token"
    __table_args__ = (
        Index("ix_email_token_user_id_purpose", "user_id", "purpose"),
        {"schema": "app"},
    )

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app.user.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    # CITEXT to match `app.user.email`, so the uniqueness question this address is put to at
    # confirm time is asked in the same case-insensitive terms the constraint answers in.
    new_email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# The follow graph (M3, D-10, EF-14). One table now, keyed by the user and the thing, with no
# surrogate id: nothing outside these rows ever needs to name one, the routes address them by
# `(entity_type, entity_id)`, and the composite key *is* the "unique per (user, type, id)" rule
# rather than a second constraint beside it.

FOLLOW_ENTITY_TYPES = ("person", "company", "franchise", "title")
FOLLOW_SOURCES = ("manual", "letterboxd_import", "tmdb_import", "derived")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Follow(Base):
    """A user's standing interest in a person, company, franchise or title (D-10, D-42).

    **The only thing a user keeps** (M8, ADR-0018, EF-14). It feeds the timeline, the digest,
    the calendar and the iCal feed — every one of them a query over this table
    (`app.follow_queries`) — so a row here is both "show me this on my timeline" and "tell me
    when something happens to it", and deleting it is the only way to stop either.

    **The row carries no preference at all (EF-1).** A follow is binary: the user follows the
    entity or they do not, and every credit of a followed person reaches them (EF-2). The
    `coverage` tier D-43 spent here is dropped — a tier control was one more thing to get
    wrong on the way to the thing the user actually asked for, and the narrow default was
    silently deciding what they would never hear about.

    `entity_id` is text because the four entity types do not share an id space: people,
    companies and franchises are TMDB integer ids (`catalog.person`, `catalog.production_company`
    and `catalog.collection` use them as primary keys), while a title is a `catalog.film` row,
    whose id is our UUID. One polymorphic column, rendered as the id the API already exposes for
    that entity, beats four nullable FK columns with a CHECK that exactly one is set: the row is
    read by entity type every time anyway (the timeline filter, the sweep's followed set), and
    the DTO normalises the value on the way in so `"012"` and `"12"` cannot become two follows.
    The cost is that
    the catalog cannot cascade a deletion into this table — acceptable, because films are never
    deleted (spec §4.4) and people, companies and collections are only ever upserted."""

    __tablename__ = "follow"
    __table_args__ = (
        CheckConstraint(
            f"entity_type IN ({_in_list(FOLLOW_ENTITY_TYPES)})", name="ck_follow_entity_type"
        ),
        CheckConstraint(f"source IN ({_in_list(FOLLOW_SOURCES)})", name="ck_follow_source"),
        # The sweep's followed set (EF-2) and the timeline (D-11) ask "who follows this entity?",
        # the reverse of the primary key's "what does this user follow?".
        Index("ix_follow_entity", "entity_type", "entity_id"),
        {"schema": "app"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app.user.id", ondelete="CASCADE"), primary_key=True
    )
    entity_type: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# The import jobs (D-15, D-16). A Letterboxd or TMDB import is minutes of TMDB requests for a
# library of any size, so it cannot run inside the upload request: the row below is what the
# upload returns instead, and what the onboarding UI polls (NEU-1358).

IMPORT_SOURCES = ("letterboxd", "tmdb")
IMPORT_STATUSES = ("queued", "running", "awaiting_review", "succeeded", "failed")
ACTIVE_IMPORT_STATUSES = ("queued", "running", "awaiting_review")
"""The statuses that count as "this user already has an import going" — the set the partial
unique index below is built on, and the one `import_job_repo.active_for_user` asks about.

`awaiting_review` is in it (EF-22) so that a user holds at most one unconfirmed list, but it is
not a reason to refuse a new import the way the other two are: starting one *discards* the
job waiting on review (`ingest.imports.review.discard_unconfirmed`) and only then asks whether
anything is still going. The index is what makes that one-list rule true under a race."""

IMPORT_SUPERSEDED = "superseded"
"""`import_job.error` on a job discarded while it was `awaiting_review`, because the user
started another import instead of confirming it (EF-22). `failed` plus this, rather than a
sixth status, because every client already renders `failed` as terminal and nothing about a
superseded list needs rendering differently — the user is looking at its replacement."""

IMPORT_SKIP_REASONS = ("outside_window",)
"""`import_candidate.skip_reason`: why a matched film is listed but not selectable (EF-21)."""


class ImportJob(Base):
    """One user's import of their library from another service, and its progress (D-15).

    Shaped after `ingest.ingest_run` — a row created up front, moved through a status, always
    finalized — but deliberately its own table rather than a `kind` on that one. The two are
    read by different people for different reasons: `ingest_run` is operational, unscoped to
    any user and surfaced to an operator through `/admin/runs`, while this row belongs to the
    user who made it, is returned to them by `GET /me/import/{id}`, and carries counts that
    only mean something for an import. Putting a user_id and four import counters on
    `ingest_run` would make every column nullable for one half of its rows.

    `unmatched` is the report the spec asks for, and it is on the row rather than derived: a
    title the resolver could not place is not an error and not a retry — it is the one thing
    the user has to act on by hand, so it has to survive the job finishing and outlive the
    poll that happened to observe it.

    The parsed rows are deliberately *not* stored. They live in memory, handed to the task the
    upload spawns, because the task runs in the same process that parsed them: persisting a
    thousand-row library as JSONB to pass it between two frames of one process would be the
    largest column in the `app` schema, written once and read once. The cost is that a job
    orphaned by a restart cannot resume — but neither can `ingest_run`'s, for the same reason,
    and the user's remedy is the same upload they already have."""

    __tablename__ = "import_job"
    __table_args__ = (
        CheckConstraint(f"source IN ({_in_list(IMPORT_SOURCES)})", name="ck_import_job_source"),
        CheckConstraint(f"status IN ({_in_list(IMPORT_STATUSES)})", name="ck_import_job_status"),
        # "One running job per user" (spec §1) as a constraint rather than only as the check
        # the route makes before inserting. The route's check answers with a clean 409, which
        # is what the caller should see; this is what makes the answer true when two uploads
        # arrive together, which a read-then-insert cannot be on its own.
        Index(
            "uq_import_job_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text(f"status IN ({_in_list(ACTIVE_IMPORT_STATUSES)})"),
        ),
        # The user's own history, newest first — the only way this table is read by anyone but
        # the job's own runner.
        Index("ix_import_job_user_id_created_at", "user_id", "created_at"),
        {"schema": "app"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app.user.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rows_done: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    watchlist_created: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    follows_created: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # `[{"name": str, "year": int | None, "kind": ...}]`, in the order the rows were read —
    # `ingest.imports.apply.UnmatchedKind` is the authority on the kinds. A list rather than a
    # table of its own: it is written once by the runner, read whole by the one route that
    # renders it, and never queried across users. The column keeps both halves of the report;
    # `app.dto.ImportJobOut` is what splits it into `unmatched` and `skipped` on the way out.
    # Rows written before M5 can carry `kind="rating"`, which nothing writes any more (EF-20).
    unmatched: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The TMDB account this job read, for the UI's "Imported from @user" (D-16). NULL for a
    # Letterboxd job, which has no account behind it.
    #
    # The *only* thing kept about that account, and deliberately: the one-shot design deletes
    # the session id when the job ends, so nothing here can read the account again. The
    # username is what lets the settings screen say which account was imported without a
    # credential, and it is the whole of NEU-1359's replacement for the unlink affordance the
    # spec removed (§4).
    tmdb_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ImportCandidate(Base):
    """One film an import proposes, waiting for the user to confirm it (EF-22).

    The job resolves and upserts as it always did, then stops at `awaiting_review` with one of
    these per matched film instead of writing follows: nothing is followed until the user has
    seen the list. `POST /me/import/{id}/confirm` writes a title follow for each id it is sent
    that is selectable here, and deletes the rows — they are a proposal, and once it has been
    answered the follows are the record. A superseded or failed job loses them the same way, so
    the table only ever holds lists somebody could still confirm.

    `selected` is the tick the review list opens with: true for a film inside the alert window,
    false with a `skip_reason` for one outside it (EF-21), which is listed so the user can see
    what the import declined rather than wonder where the title went. A skipped row is never
    selectable, whatever the client sends.

    **Matched films only.** A title the import could not place has no film to point at, and
    stays in `import_job.unmatched` exactly as NEU-1448 left it: that list tells the user to go
    and follow something by hand, which is the opposite of what this one asks.

    `title` and `headline_release` are snapshots taken when the row is written, so the review
    list renders from this table alone. `title` is the catalog's rather than the export's, on
    purpose: a Letterboxd row is matched by a search rule, and the catalog's title is how the
    user catches a wrong match before it becomes a follow."""

    __tablename__ = "import_candidate"
    __table_args__ = (
        CheckConstraint(
            f"skip_reason IS NULL OR skip_reason IN ({_in_list(IMPORT_SKIP_REASONS)})",
            name="ck_import_candidate_skip_reason",
        ),
        CheckConstraint(
            "skip_reason IS NULL OR NOT selected", name="ck_import_candidate_skipped_unselected"
        ),
        # One row per film per job: two export rows that resolve to the same film are one
        # proposal, and one follow if confirmed. Also the index the confirm and the read use.
        Index("uq_import_candidate_job_film", "job_id", "film_id", unique=True),
        {"schema": "app"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app.import_job.id", ondelete="CASCADE"), nullable=False
    )
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("catalog.film.id", ondelete="CASCADE"), nullable=False
    )
    tmdb_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # `app.dto.HeadlineReleaseOut` as JSON, or NULL for a film with no date to lead with.
    headline_release: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class TmdbAuthRequest(Base):
    """A TMDB request token this user has been sent off to approve, waiting for them to come
    back (D-16).

    The whole of the state the approve flow keeps, and it lives for fifteen minutes. TMDB's
    redirect carries the request token and nothing else — no user, no signature — so without a
    row saying who was sent which token, the callback would have to take the caller's word for
    it and an approved token could be replayed against another account. The row is the binding,
    and it is deleted the moment it is spent.

    Note what is *not* here: the session id that token becomes. The original ticket stored one
    per user, encrypted; the spec replaced that with a one-shot import whose last act is to
    delete the session at TMDB, so the only long-lived credential in this flow is the one that
    no longer exists. This table holds a value that is worthless without the user's browser
    and expires on its own."""

    __tablename__ = "tmdb_auth_request"
    __table_args__ = (
        # Pruning reads "everything older than fifteen minutes" across all users, which the
        # primary key on the token cannot answer.
        Index("ix_tmdb_auth_request_created_at", "created_at"),
        {"schema": "app"},
    )

    request_token: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app.user.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# Delivery (M7, D-31 to D-34). The per-user settings the digest and the calendar feed read, and
# the queue the decision pass writes.

DIGEST_CADENCES = ("daily", "weekly", "off")
DEFAULT_DIGEST_CADENCE = "weekly"  # D-33; mirrored by the column's server default
NOTIFICATION_KINDS = ("digest",)  # ADR-0021: the digest is the only delivery
NOTIFICATION_CHANNELS = ("email",)
NOTIFICATION_STATUSES = ("queued", "sent", "failed", "suppressed")


class UserSettings(Base):
    """One user's delivery preferences: how often they want the digest, and the token their
    calendar subscribes with (D-33, D-34). There is no per-beat preference: the digest carries
    everything the timeline carries, and a reader who wants less news unfollows (ADR-0021).

    Keyed by the user with no surrogate id, like the follow graph next door: there is one row per
    user by definition and nothing ever names it another way.

    **The row is created lazily, on first read** (`settings_service.get_or_create`), rather than
    at signup. Signup would have to write a row for every account including the ones that never
    reach a settings screen, and a backfill would have to invent one for every account that
    already exists; a default-valued row that no one has looked at yet carries no information
    that the defaults do not. Because every route that reaches this table is behind
    `require_entitled()` (D-39), the row only ever appears for a user who holds a grant — and
    D-40 keeps it, token included, when that grant lapses, so a renewed subscription resumes on
    the same calendar URL rather than silently breaking one the user has already added to their
    phone.

    `ical_token` is `NOT NULL` and unique: it is the whole of `/calendar/{token}.ics`'s lookup,
    so a duplicate would hand one user another's dates, and a NULL would be a settings row whose
    calendar link cannot be rendered. Rotating it is a plain update (D-34) — the old value stops
    resolving the moment it is overwritten, which is the point of the affordance."""

    __tablename__ = "user_settings"
    __table_args__ = (
        CheckConstraint(
            f"digest_cadence IN ({_in_list(DIGEST_CADENCES)})",
            name="ck_user_settings_digest_cadence",
        ),
        {"schema": "app"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app.user.id", ondelete="CASCADE"), primary_key=True
    )
    digest_cadence: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text(f"'{DEFAULT_DIGEST_CADENCE}'")
    )
    ical_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    unsubscribe_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    """The digest's one-click unsubscribe credential (DC-10): `/digest/unsubscribe/{token}`
    turns this user's digest off. Unique for `ical_token`'s reason — it is the whole lookup.
    Unlike that one it never rotates and is never returned by `/me/settings`: all it can do is
    stop a mail, and it travels in every digest's headers."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Notification(Base):
    """One decision to tell one user about one event, and what became of it (D-31).

    Written by the decision pass over newly published events — never by ingest, and never by a
    route — which is why nothing in this ticket inserts here: the pass that does is NEU-1379.
    The row exists before the mail does, so a send that fails is a `failed` row with its reason
    rather than a silence, and a recipient who must not be mailed at all (unverified, D-31; or
    unentitled, D-39) is a `suppressed` row rather than an absence that looks the same as the
    pass never having considered them.

    The unique key on `(user_id, event_id, kind, channel)` is what makes that pass re-runnable:
    it selects newly published events by `created_at` since the last pass, so a pass that is
    run twice, or whose window overlaps after a crash, reconsiders events it has already
    decided. The key turns the second decision into a conflict to ignore instead of a second
    mail. `kind` and `channel` are in it from when an event could also earn an alert and a
    push; ADR-0021 retired both, so each has one value now and the key is one row per
    `(user, event)` in practice. The columns stay rather than churn the row's shape.

    `event_id` cascades: an event deleted from the ledger takes its delivery decisions with it.
    That is history the ledger no longer has a subject for, and notifications are not the claim
    record — `news.event` is (ADR-0017)."""

    __tablename__ = "notification"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_in_list(NOTIFICATION_KINDS)})", name="ck_notification_kind"),
        CheckConstraint(
            f"channel IN ({_in_list(NOTIFICATION_CHANNELS)})", name="ck_notification_channel"
        ),
        CheckConstraint(
            f"status IN ({_in_list(NOTIFICATION_STATUSES)})", name="ck_notification_status"
        ),
        Index(
            "uq_notification_user_event_kind_channel",
            "user_id",
            "event_id",
            "kind",
            "channel",
            unique=True,
        ),
        # The sender's own question: the backlog, oldest first. Not partial on
        # `status = 'queued'`, so the operational read that follows a bad run — "what failed,
        # and when?" — is served by the same index.
        Index("ix_notification_status_created_at", "status", "created_at"),
        {"schema": "app"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app.user.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("news.event.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why a `failed` row failed, as the sender saw it. Free text rather than a code: it is read
    # by a person looking at a row that did not go out, and the provider's own message is the
    # most useful thing to put in front of them.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
