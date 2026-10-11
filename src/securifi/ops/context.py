"""Who is calling, through which entry point, and the per-call context handed to handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy.orm import Session


class Channel(str, Enum):
    UI = "ui"
    API = "api"
    MCP = "mcp"
    CLI = "cli"
    SYSTEM = "system"  # scheduled jobs, bootstrap and other internal callers


@dataclass(frozen=True)
class Actor:
    id: str | None
    label: str
    role: str
    permissions: frozenset[str]
    facility_scope: frozenset[str] | None = None  # None = all facilities
    api_key_id: str | None = None
    is_service_account: bool = False

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def can_access_facility(self, facility_id: str | None) -> bool:
        return facility_id is None or self.facility_scope is None or facility_id in self.facility_scope


def system_actor() -> Actor:
    from .permissions import PERMISSIONS

    return Actor(id=None, label="system", role="admin", permissions=frozenset(PERMISSIONS), is_service_account=True)


@dataclass
class OpContext:
    actor: Actor
    channel: Channel
    session: Session
    request_id: str | None = None
    dry_run: bool = False
    # Handlers add ids of entities they touched; these are written to the audit record.
    entity_refs: list[str] = field(default_factory=list)
    facility_id: str | None = None

    def touch(self, *ids: str | None) -> None:
        self.entity_refs.extend(i for i in ids if i)

    def require(self, permission: str) -> None:
        from .errors import PermissionDenied

        if not self.actor.can(permission):
            raise PermissionDenied(f"'{self.actor.label}' lacks permission '{permission}'")

    def bind_facility(self, facility_id: str) -> None:
        """Called by handlers once they resolve a facility, enforcing facility scoping."""
        from .errors import PermissionDenied

        if not self.actor.can_access_facility(facility_id):
            raise PermissionDenied(f"'{self.actor.label}' has no access to facility {facility_id}")
        self.facility_id = facility_id
