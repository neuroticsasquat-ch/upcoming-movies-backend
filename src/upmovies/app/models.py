from datetime import datetime
from uuid import UUID

from sqlalchemy import (  # noqa: I001
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET
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
    for every cause — so this costs forensic precision and no behaviour."""

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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
