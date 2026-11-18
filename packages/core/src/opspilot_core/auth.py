import enum
from datetime import UTC, datetime, timedelta

import jwt
from pydantic import BaseModel

from opspilot_core.config import CoreSettings


class Role(enum.StrEnum):
    STAFF = "staff"
    MANAGER = "manager"
    ADMIN = "admin"


class User(BaseModel):
    id: str
    role: Role
    department: str


class AuthError(Exception):
    pass


def issue_token(user: User, settings: CoreSettings) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user.id,
        "role": user.role.value,
        "dept": user.department,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str, settings: CoreSettings) -> User:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return User(id=payload["sub"], role=Role(payload["role"]), department=payload["dept"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise AuthError("invalid token") from exc


def approval_denied_reason(approver: User, *, requested_by: str, department: str) -> str | None:
    """Four-eyes rule shared by every approval in OpsPilot. None means the approver may decide."""
    if approver.role not in (Role.MANAGER, Role.ADMIN):
        return "only managers or admins can approve"
    if approver.id == requested_by:
        return "requester cannot approve their own request"
    if approver.role is Role.MANAGER and approver.department != department:
        return "manager must belong to the requesting department"
    return None
