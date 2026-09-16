from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


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
    invite_code: str = Field(min_length=1, max_length=128)


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
