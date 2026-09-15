from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)
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
