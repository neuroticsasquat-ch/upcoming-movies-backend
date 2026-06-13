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


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str
    is_admin: bool
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
