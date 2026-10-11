"""Tamper-evident audit log.

Each event stores the hash of the previous event; the event hash covers its own content and
that previous hash. Editing or deleting any historical row breaks every hash after it, which
``verify_chain`` detects.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import to_jsonable, utcnow
from ..models import AuditChainHead, AuditEvent

GENESIS = "0" * 64
_SENSITIVE = ("password", "secret", "token", "api_key", "account_number", "payment_details")
_MAX_STRING = 2000


def redact(value: Any, key: str = "") -> Any:
    if any(s in key.lower() for s in _SENSITIVE):
        return "***"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > 50:
            return {"_truncated_list": len(value), "sha256": _digest(value)}
        return [redact(v, key) for v in value]
    if isinstance(value, str) and len(value) > _MAX_STRING:
        return {"_truncated_text": len(value), "sha256": hashlib.sha256(value.encode()).hexdigest()}
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(to_jsonable(value), sort_keys=True, default=str).encode()).hexdigest()


def _ts(value: datetime) -> str:
    """Timestamp text that survives a database round trip (SQLite drops the timezone)."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def _payload(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "occurred_at": _ts(event.occurred_at),
        "actor_id": event.actor_id,
        "actor_label": event.actor_label,
        "api_key_id": event.api_key_id,
        "channel": event.channel,
        "operation": event.operation,
        "mutating": event.mutating,
        "facility_id": event.facility_id,
        "outcome": event.outcome,
        "params": event.params,
        "result_summary": event.result_summary,
        "error": event.error,
        "entity_refs": event.entity_refs,
        "request_id": event.request_id,
    }


def compute_hash(event: AuditEvent, prev_hash: str) -> str:
    body = json.dumps(to_jsonable(_payload(event)), sort_keys=True, default=str)
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


def append(session: Session, **fields: Any) -> AuditEvent:
    """Append an event inside the caller's transaction (so it commits or rolls back with it)."""
    head = session.execute(select(AuditChainHead).where(AuditChainHead.id == 1).with_for_update()).scalar_one_or_none()
    if head is None:
        head = AuditChainHead(id=1, last_hash=GENESIS, last_seq=0)
        session.add(head)
        session.flush()
    fields.setdefault("occurred_at", utcnow())
    fields["params"] = to_jsonable(redact(fields.get("params") or {}))
    fields["result_summary"] = to_jsonable(fields.get("result_summary")) if fields.get("result_summary") else None
    event = AuditEvent(**fields)
    if not event.id:
        from ..db import new_id

        event.id = new_id("aud")
    event.prev_hash = head.last_hash
    event.hash = compute_hash(event, head.last_hash)
    session.add(event)
    session.flush()
    head.last_hash = event.hash
    head.last_seq = event.seq
    return event


def verify_chain(session: Session) -> dict[str, Any]:
    prev = GENESIS
    checked = 0
    for event in session.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars():
        if event.prev_hash != prev or compute_hash(event, prev) != event.hash:
            return {"valid": False, "events_checked": checked, "first_invalid_seq": event.seq, "first_invalid_id": event.id}
        prev = event.hash
        checked += 1
    head = session.get(AuditChainHead, 1)
    if head is not None and head.last_hash != prev:
        return {"valid": False, "events_checked": checked, "reason": "chain head does not match last event (rows deleted?)"}
    return {"valid": True, "events_checked": checked, "head_hash": prev}
