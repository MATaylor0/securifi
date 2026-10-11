"""API key issuance and authentication (shared by UI, API, MCP and CLI)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import ApiKey, User
from .context import Actor
from .errors import Unauthenticated
from .permissions import permissions_for

KEY_PREFIX = "sfk_"


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_key(session: Session, user: User, name: str, scopes: list[str] | None = None,
              expires_at: datetime | None = None) -> tuple[ApiKey, str]:
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    key = ApiKey(user_id=user.id, name=name, prefix=raw[:12], key_hash=hash_key(raw), scopes=scopes, expires_at=expires_at)
    session.add(key)
    session.flush()
    return key, raw


def actor_for_user(user: User, key: ApiKey | None = None) -> Actor:
    return Actor(
        id=user.id,
        label=user.email,
        role=user.role,
        permissions=permissions_for(user.role, key.scopes if key else None),
        facility_scope=frozenset(user.facility_scope) if user.facility_scope is not None else None,
        api_key_id=key.id if key else None,
        is_service_account=user.is_service_account,
    )


def authenticate(session: Session, raw_key: str | None) -> Actor:
    if not raw_key:
        raise Unauthenticated("an API key is required (Authorization: Bearer <key>)")
    raw_key = raw_key.strip()
    if raw_key.lower().startswith("bearer "):
        raw_key = raw_key[7:].strip()
    key = session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))).scalar_one_or_none()
    if key is None or key.revoked_at is not None:
        raise Unauthenticated("invalid or revoked API key")
    if key.expires_at is not None:
        expires = key.expires_at if key.expires_at.tzinfo else key.expires_at.replace(tzinfo=timezone.utc)
        if expires < utcnow():
            raise Unauthenticated("API key has expired")
    user = session.get(User, key.user_id)
    if user is None or not user.active:
        raise Unauthenticated("user is inactive")
    key.last_used_at = utcnow()
    return actor_for_user(user, key)
