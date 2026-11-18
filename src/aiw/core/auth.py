import enum
from datetime import UTC, datetime, timedelta

import jwt
from pydantic import BaseModel

from aiw.core.config import Settings


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


def issue_token(user: User, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user.id,
        "role": user.role.value,
        "dept": user.department,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str, settings: Settings) -> User:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return User(id=payload["sub"], role=Role(payload["role"]), department=payload["dept"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise AuthError("invalid token") from exc
