import hashlib
import re
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request

import db
import settings

COOKIE = "session"
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
MIN_PASSWORD = 10

_hasher = PasswordHasher()
# Verified against when a username does not exist, so login timing does not reveal valid usernames.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY_HASH, password) and stored is not None
    except (VerificationError, InvalidHashError):
        return False


def check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise HTTPException(422, f"Password must be at least {MIN_PASSWORD} characters.")
    if len(password) > 200:
        raise HTTPException(422, "Password is too long.")


def temp_password() -> str:
    return secrets.token_urlsafe(12)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    """Sliding window, in memory (per Cloud Run instance)."""

    def __init__(self, limit: int, window: int):
        self.limit, self.window, self.hits = limit, window, defaultdict(deque)

    def check(self, key: str, message: str = "Too many requests, slow down.") -> None:
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise HTTPException(429, message)
        q.append(now)


async def create_session(user_id, ip: str, user_agent: str) -> str:
    token = secrets.token_urlsafe(32)
    await db.execute(
        f"insert into {db.S}.sessions (user_id, token_hash, expires_at, ip, user_agent) "
        f"values (%s, %s, now() + make_interval(days => %s), %s, %s)",
        user_id, _token_hash(token), settings.SESSION_DAYS, ip, user_agent[:300],
    )
    await db.execute(f"delete from {db.S}.sessions where expires_at < now()")
    return token


async def delete_session(token: str) -> None:
    await db.execute(f"delete from {db.S}.sessions where token_hash = %s", _token_hash(token))


async def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE)
    user = token and await db.fetchone(
        f"select u.id, u.username, u.display_name, u.role, u.must_change_password "
        f"from {db.S}.sessions s join {db.S}.users u on u.id = s.user_id "
        f"where s.token_hash = %s and s.expires_at > now() and u.is_active",
        _token_hash(token),
    )
    if not user:
        raise HTTPException(401, "Not signed in.")
    return user


async def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only.")
    return user
