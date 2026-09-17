from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


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
    code: str
    email_hint: str | None
    created_at: datetime
    consumed_at: datetime | None
    consumed_by_user_id: UUID | None


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


# --- follows and the watchlist (M3, D-10 to D-14) ---------------------------------------------

FollowEntityType = Literal["person", "company", "franchise", "title"]
AlertPref = Literal["buy", "rent", "stream"]


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
    entity_type: FollowEntityType
    entity_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _normalise(self) -> "FollowCreateRequest":
        self.entity_id = normalise_entity_id(self.entity_type, self.entity_id)
        return self


class FollowOut(BaseModel):
    entity_type: str
    entity_id: str
    source: str
    created_at: datetime


class FollowListResponse(BaseModel):
    items: list[FollowOut]


def normalise_alert_prefs(prefs: list[str]) -> list[str]:
    """Canonical order, no duplicates, so two prefs lists that mean the same thing compare
    equal and the row reads the same however the client spelled it."""
    return [p for p in ("buy", "rent", "stream") if p in prefs]


class WatchlistFilmOut(BaseModel):
    """Enough of the film to render a watchlist row without a second request per item. The
    film page is the place for the rest."""

    id: UUID
    tmdb_id: int
    slug: str | None
    title: str
    poster_path: str | None
    release_date: date | None


class WatchlistCreateRequest(BaseModel):
    film_id: UUID
    # Omitted means the D-14 default, `{stream}`; an explicit empty list means "no availability
    # alerts", which is a different thing and is honoured.
    alert_prefs: list[AlertPref] | None = None

    @field_validator("alert_prefs")
    @classmethod
    def _normalise(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else normalise_alert_prefs(v)


class WatchlistUpdateRequest(BaseModel):
    alert_prefs: list[AlertPref]

    @field_validator("alert_prefs")
    @classmethod
    def _normalise(cls, v: list[str]) -> list[str]:
        return normalise_alert_prefs(v)


class WatchlistItemOut(BaseModel):
    film: WatchlistFilmOut
    source: str
    alert_prefs: list[str]
    created_at: datetime


class WatchlistListResponse(BaseModel):
    items: list[WatchlistItemOut]
