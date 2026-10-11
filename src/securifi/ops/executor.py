"""The one pathway every operation runs through, whichever entry point invoked it.

    entry point (UI / API / MCP / CLI / system)
        -> execute(name, params, actor, channel)
            1. resolve the operation in the registry
            2. check the actor's permission
            3. validate params against the operation's input model
            4. run the handler in a database transaction
            5. append a hash-chained audit event in the same transaction
            6. commit (or roll back for dry runs and failures, auditing the failure separately)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..db import new_session, to_jsonable
from ..models import IdempotencyRecord
from . import audit
from .context import Actor, Channel, OpContext
from .errors import Conflict, NotFound, OpError, PermissionDenied, ValidationFailed
from .registry import Operation, get_operation

log = logging.getLogger("securifi.ops")


@dataclass
class OpResult:
    ok: bool
    operation: str
    data: Any = None
    error: dict[str, Any] | None = None
    status: int = 200
    audit_id: str | None = None
    dry_run: bool = False
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok, "operation": self.operation, "request_id": self.request_id, "audit_id": self.audit_id}
        if self.dry_run:
            out["dry_run"] = True
        if self.ok:
            out["data"] = to_jsonable(self.data)
        else:
            out["error"] = to_jsonable(self.error)
        return out


def _summarise(data: Any) -> dict[str, Any] | None:
    """Small summary of a result for the audit record (ids and counts, not full payloads)."""
    if isinstance(data, dict):
        summary = {k: v for k, v in data.items() if k in ("id", "code", "status", "version", "count", "run_number") or k.endswith("_id")}
        for k, v in data.items():
            if isinstance(v, list):
                summary[f"{k}_count"] = len(v)
        return summary or None
    if isinstance(data, list):
        return {"count": len(data)}
    return None


def execute(
    name: str,
    params: dict[str, Any] | None,
    actor: Actor,
    channel: Channel | str,
    *,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    request_id: str | None = None,
    session: Session | None = None,
) -> OpResult:
    channel = Channel(channel)
    request_id = request_id or uuid.uuid4().hex
    params = dict(params or {})
    op = get_operation(name)
    if op is None:
        err = NotFound(f"unknown operation '{name}'")
        return OpResult(False, name, error=err.to_dict(), status=err.status, request_id=request_id)

    owns_session = session is None
    session = session or new_session()
    base_audit = dict(
        request_id=request_id, actor_id=actor.id, actor_label=actor.label, api_key_id=actor.api_key_id,
        channel=channel.value, operation=op.name, mutating=op.mutating, params=params,
    )
    ctx = OpContext(actor=actor, channel=channel, session=session, request_id=request_id, dry_run=dry_run)
    ctx.facility_id = _facility_hint(session, params)
    try:
        if idempotency_key and op.mutating and not dry_run:
            existing = session.get(IdempotencyRecord, idempotency_key)
            if existing is not None:
                if existing.operation != op.name or existing.actor_id != actor.id:
                    raise Conflict("idempotency key was already used for a different request")
                return OpResult(True, op.name, data=existing.result.get("data"), audit_id=existing.result.get("audit_id"), request_id=request_id)
        if not actor.can(op.permission):
            raise PermissionDenied(f"'{actor.label}' lacks permission '{op.permission}' required by {op.name}")
        try:
            validated = op.input_model.model_validate(params)
        except ValidationError as exc:
            raise ValidationFailed("invalid parameters", details=_validation_details(exc)) from exc

        data = op.handler(ctx, validated)
        session.flush()
        jsonable = to_jsonable(data)
        refs = ctx.entity_refs or None
        if dry_run and op.mutating:
            # Discard every change the handler made, then record that a dry run happened.
            session.rollback()
            audit_id = with_session_audit(session, **base_audit, facility_id=ctx.facility_id, outcome="dry_run",
                                          result_summary=_summarise(jsonable), entity_refs=refs)
            return OpResult(True, op.name, data=jsonable, audit_id=audit_id, dry_run=True, request_id=request_id)
        event = audit.append(session, **base_audit, facility_id=ctx.facility_id, outcome="success",
                             result_summary=_summarise(jsonable), entity_refs=refs)
        if idempotency_key and op.mutating:
            session.add(IdempotencyRecord(key=idempotency_key, actor_id=actor.id, operation=op.name,
                                          result={"data": jsonable, "audit_id": event.id}))
        session.commit()
        return OpResult(True, op.name, data=jsonable, audit_id=event.id, request_id=request_id)
    except OpError as exc:
        session.rollback()
        audit_id = with_session_audit(session, **base_audit, facility_id=ctx.facility_id, outcome=exc.outcome,
                                      error=f"{exc.code}: {exc.message}")
        return OpResult(False, op.name, error=exc.to_dict(), status=exc.status, audit_id=audit_id, request_id=request_id)
    except Exception as exc:  # unexpected - never leak internals, always audit
        log.exception("operation %s failed", op.name)
        session.rollback()
        audit_id = with_session_audit(session, **base_audit, facility_id=ctx.facility_id, outcome="failed",
                                      error=f"internal_error: {type(exc).__name__}: {exc}")
        return OpResult(False, op.name, error={"code": "internal_error", "message": "unexpected error; see audit log", "request_id": request_id},
                        status=500, audit_id=audit_id, request_id=request_id)
    finally:
        if owns_session:
            session.close()


def _facility_hint(session: Session, params: dict[str, Any]) -> str | None:
    """Resolve the facility named in raw params so even denied or invalid calls are attributed to it."""
    ref = params.get("facility_id")
    if not isinstance(ref, str) or not ref:
        return None
    from sqlalchemy import or_, select

    from ..models import Facility

    try:
        return session.execute(select(Facility.id).where(or_(Facility.id == ref, Facility.code == ref))).scalar_one_or_none()
    except Exception:  # pragma: no cover - never let attribution break execution
        return None


def with_session_audit(session: Session, **fields: Any) -> str | None:
    try:
        event = audit.append(session, **fields)
        session.commit()
        return event.id
    except Exception:  # pragma: no cover - audit store unavailable
        log.exception("failed to write audit event")
        session.rollback()
        return None


def _validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    return [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]


def describe(op: Operation) -> dict[str, Any]:
    return op.describe()
