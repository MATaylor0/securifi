"""Users, API keys and the audit log."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from ...db import utcnow
from ...models import ApiKey, AuditEvent, User
from .. import audit
from ..auth import issue_key
from ..context import OpContext
from ..errors import Conflict, NotFound, ValidationFailed
from ..permissions import PERMISSIONS, ROLE_DESCRIPTIONS, ROLES
from ..registry import NoInput, OpInput, operation
from .common import to_dict

Role = Literal["admin", "trust_manager", "analyst", "agent", "auditor", "viewer"]


def _user_dict(u: User) -> dict:
    return to_dict(u)


@operation("auth.whoami", summary="Describe the calling identity and its permissions", permission="facility.read", mutating=False, tags=("admin",))
def whoami(ctx: OpContext, _: NoInput) -> dict:
    a = ctx.actor
    return {
        "user_id": a.id, "label": a.label, "role": a.role, "channel": ctx.channel.value,
        "permissions": sorted(a.permissions),
        "facility_scope": sorted(a.facility_scope) if a.facility_scope is not None else None,
        "api_key_id": a.api_key_id,
    }


@operation("admin.roles", summary="List roles and the permissions each grants", permission="facility.read", mutating=False, tags=("admin",))
def list_roles(ctx: OpContext, _: NoInput) -> dict:
    return {
        "roles": [{"role": r, "description": ROLE_DESCRIPTIONS[r], "permissions": sorted(p)} for r, p in ROLES.items()],
        "permissions": PERMISSIONS,
    }


class UserCreate(OpInput):
    email: str = Field(min_length=3, max_length=255)
    name: str
    role: Role
    facility_scope: list[str] | None = Field(None, description="Restrict to these facility ids; omit for all facilities")
    is_service_account: bool = Field(False, description="True for AI agents and integrations")


@operation("admin.user_create", summary="Create a user or service account", permission="admin.users", input=UserCreate, mutating=True)
def user_create(ctx: OpContext, p: UserCreate) -> dict:
    if ctx.session.execute(select(User).where(User.email == p.email)).scalar_one_or_none():
        raise Conflict(f"user {p.email} already exists")
    user = User(**p.model_dump())
    ctx.session.add(user)
    ctx.session.flush()
    ctx.touch(user.id)
    return _user_dict(user)


class UserUpdate(OpInput):
    user_id: str
    role: Role | None = None
    name: str | None = None
    active: bool | None = None
    facility_scope: list[str] | None = None
    clear_facility_scope: bool = False


@operation("admin.user_update", summary="Change a user's role, scope or active flag", permission="admin.users", input=UserUpdate, mutating=True)
def user_update(ctx: OpContext, p: UserUpdate) -> dict:
    user = ctx.session.get(User, p.user_id)
    if user is None:
        raise NotFound("user not found")
    if user.id == ctx.actor.id and (p.active is False or (p.role and p.role != "admin")):
        raise ValidationFailed("you cannot deactivate or demote yourself")
    for f in ("role", "name", "active", "facility_scope"):
        v = getattr(p, f)
        if v is not None:
            setattr(user, f, v)
    if p.clear_facility_scope:
        user.facility_scope = None
    ctx.touch(user.id)
    return _user_dict(user)


@operation("admin.user_list", summary="List users", permission="admin.users", mutating=False)
def user_list(ctx: OpContext, _: NoInput) -> list:
    return [_user_dict(u) for u in ctx.session.execute(select(User).order_by(User.email)).scalars()]


class KeyCreate(OpInput):
    user_id: str
    name: str = Field(description="Label, e.g. 'Claude desktop' or 'nightly ingestion job'")
    scopes: list[str] | None = Field(None, description="Optional subset of the user's permissions for this key")
    expires_at: datetime | None = None


@operation("admin.apikey_create", summary="Issue an API key (shown once)", permission="admin.users", input=KeyCreate, mutating=True)
def apikey_create(ctx: OpContext, p: KeyCreate) -> dict:
    user = ctx.session.get(User, p.user_id)
    if user is None:
        raise NotFound("user not found")
    if p.scopes:
        unknown = set(p.scopes) - set(PERMISSIONS)
        if unknown:
            raise ValidationFailed(f"unknown scopes: {sorted(unknown)}")
    key, raw = issue_key(ctx.session, user, p.name, p.scopes, p.expires_at)
    ctx.touch(key.id, user.id)
    return {"id": key.id, "user_id": user.id, "name": key.name, "prefix": key.prefix, "api_key": raw,
            "note": "Store this key securely; it cannot be shown again."}


class KeyRevoke(OpInput):
    key_id: str


@operation("admin.apikey_revoke", summary="Revoke an API key", permission="admin.users", input=KeyRevoke, mutating=True)
def apikey_revoke(ctx: OpContext, p: KeyRevoke) -> dict:
    key = ctx.session.get(ApiKey, p.key_id)
    if key is None:
        raise NotFound("key not found")
    key.revoked_at = key.revoked_at or utcnow()
    ctx.touch(key.id)
    return {"id": key.id, "revoked_at": key.revoked_at}


@operation("admin.apikey_list", summary="List API keys (hashes are never returned)", permission="admin.users", mutating=False)
def apikey_list(ctx: OpContext, _: NoInput) -> list:
    return [to_dict(k, exclude=("key_hash",)) for k in ctx.session.execute(select(ApiKey).order_by(ApiKey.created_at)).scalars()]


class AuditQuery(OpInput):
    facility_id: str | None = None
    operation: str | None = None
    actor_id: str | None = None
    channel: Literal["ui", "api", "mcp", "cli", "system"] | None = None
    outcome: Literal["success", "dry_run", "denied", "invalid", "failed"] | None = None
    mutating_only: bool = False
    limit: int = Field(100, ge=1, le=1000)
    before_seq: int | None = Field(None, description="Page backwards from this sequence number")


@operation("audit.query", summary="Search the audit log", permission="audit.read", input=AuditQuery, mutating=False, tags=("audit",))
def audit_query(ctx: OpContext, p: AuditQuery) -> list:
    q = select(AuditEvent)
    for col, val in (("facility_id", p.facility_id), ("operation", p.operation), ("actor_id", p.actor_id),
                     ("channel", p.channel), ("outcome", p.outcome)):
        if val:
            q = q.where(getattr(AuditEvent, col) == val)
    if p.mutating_only:
        q = q.where(AuditEvent.mutating.is_(True))
    if p.before_seq:
        q = q.where(AuditEvent.seq < p.before_seq)
    if ctx.actor.facility_scope is not None:
        q = q.where(AuditEvent.facility_id.in_(sorted(ctx.actor.facility_scope)))
    return [to_dict(e) for e in ctx.session.execute(q.order_by(AuditEvent.seq.desc()).limit(p.limit)).scalars()]


@operation("audit.verify", summary="Verify the integrity of the audit hash chain", permission="audit.read", mutating=False, tags=("audit",))
def audit_verify(ctx: OpContext, _: NoInput) -> dict:
    return audit.verify_chain(ctx.session)
