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
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, INET, JSONB
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


# The follow graph and the watchlist (M3, D-10 to D-14). Three tables, all keyed by the user
# and the thing, with no surrogate ids: nothing outside these rows ever needs to name one, the
# routes address them by `(entity_type, entity_id)` or `film_id`, and the composite key *is* the
# "unique per (user, type, id)" rule rather than a second constraint beside it.

FOLLOW_ENTITY_TYPES = ("person", "company", "franchise", "title")
FOLLOW_SOURCES = ("manual", "letterboxd_import", "tmdb_import", "derived")
WATCHLIST_SOURCES = ("manual", "derived_from_follow", "letterboxd_import", "tmdb_import")
ALERT_PREFS = ("buy", "rent", "stream")
DEFAULT_ALERT_PREFS = ("stream",)  # D-14; mirrored by the column's server default


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Follow(Base):
    """A user's standing interest in a person, company, franchise or title (D-10).

    A follow produces timeline rows and nothing else — never a push; that is the watchlist's
    job — so nothing here carries preferences.

    `entity_id` is text because the four entity types do not share an id space: people,
    companies and franchises are TMDB integer ids (`catalog.person`, `catalog.production_company`
    and `catalog.collection` use them as primary keys), while a title is a `catalog.film` row,
    whose id is our UUID. One polymorphic column, rendered as the id the API already exposes for
    that entity, beats four nullable FK columns with a CHECK that exactly one is set: the row is
    read by entity type every time anyway (timeline filter, derivation), and the DTO normalises
    the value on the way in so `"012"` and `"12"` cannot become two follows. The cost is that
    the catalog cannot cascade a deletion into this table — acceptable, because films are never
    deleted (spec §4.4) and people, companies and collections are only ever upserted."""

    __tablename__ = "follow"
    __table_args__ = (
        CheckConstraint(
            f"entity_type IN ({_in_list(FOLLOW_ENTITY_TYPES)})", name="ck_follow_entity_type"
        ),
        CheckConstraint(f"source IN ({_in_list(FOLLOW_SOURCES)})", name="ck_follow_source"),
        # The derivation pass (D-13) and the timeline (D-11) ask "who follows this entity?",
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


class WatchlistItem(Base):
    """A title the user wants to be *told* about — the only row in the system that produces a
    push (D-13, D-14).

    `source` records who put it here. `manual` is the user; `derived_from_follow` is the follow
    graph acting on their behalf, and is the value that makes a later removal a dismissal
    rather than a plain delete; the two import sources are named here because D-15 and D-16
    write them, and a CHECK that did not know about them would fail the import tickets at the
    constraint rather than the review.

    `alert_prefs` is the subset of `{buy, rent, stream}` availability beats this item may alert
    on; the push-whitelist beats (a date assigned or moved, a home-release date, a trailer) are
    always on for a watchlist item and are deliberately not represented here, so they cannot
    be switched off. The default `{stream}` is a server default so a row written by the
    derivation pass or an import gets it without each writer restating it."""

    __tablename__ = "watchlist_item"
    __table_args__ = (
        CheckConstraint(
            f"source IN ({_in_list(WATCHLIST_SOURCES)})", name="ck_watchlist_item_source"
        ),
        CheckConstraint(
            f"alert_prefs <@ ARRAY[{_in_list(ALERT_PREFS)}]::text[]",
            name="ck_watchlist_item_alert_prefs",
        ),
        # The notify pass (D-31) and the provider poll's scoped set (D-27) ask "who has this
        # film watchlisted?", the reverse of the primary key.
        Index("ix_watchlist_item_film_id", "film_id"),
        {"schema": "app"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app.user.id", ondelete="CASCADE"), primary_key=True
    )
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("catalog.film.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    alert_prefs: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{stream}'::text[]")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class WatchlistDismissal(Base):
    """The user removed a derived watchlist item, and the follow graph must not put it back
    (D-13).

    A row rather than a flag on the item because the item is gone: the dismissal is the only
    thing left that says the derivation already happened and was refused. Permanent by design —
    nothing deletes these, and a revoke leaves them alone (D-40) — but it binds the *derivation*
    only: the user adding the same film by hand is a `manual` item and is not blocked by it."""

    __tablename__ = "watchlist_dismissal"
    __table_args__ = {"schema": "app"}

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app.user.id", ondelete="CASCADE"), primary_key=True
    )
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("catalog.film.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# The import jobs (D-15, D-16). A Letterboxd or TMDB import is minutes of TMDB requests for a
# library of any size, so it cannot run inside the upload request: the row below is what the
# upload returns instead, and what the onboarding UI polls (NEU-1358).

IMPORT_SOURCES = ("letterboxd", "tmdb")
IMPORT_STATUSES = ("queued", "running", "succeeded", "failed")
ACTIVE_IMPORT_STATUSES = ("queued", "running")
"""The statuses that count as "this user already has an import going" — the set the partial
unique index below is built on, and the one `import_job_repo.active_for_user` asks about."""


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
    # `[{"name": str, "year": int | None, "kind": "watchlist" | "rating"}]`, in the order the
    # rows were read. A list rather than a table of its own: it is written once by the runner,
    # read whole by the one route that renders it, and never queried across users.
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
