"""Helpers shared by handlers."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, TypeVar

from sqlalchemy import inspect, or_, select

from ...engine.interest import Movement
from ...engine.money import ZERO
from ...models import Facility, NoteClass, Noteholder, RegisterEntry
from ..context import OpContext
from ..errors import NotFound

T = TypeVar("T")

PRINCIPAL_SIGN = {"drawdown": 1, "transfer_in": 1, "repayment": -1, "transfer_out": -1}


def to_dict(obj: Any, exclude: Iterable[str] = ()) -> dict[str, Any]:
    skip = set(exclude)
    return {c.key: getattr(obj, c.key) for c in inspect(obj).mapper.column_attrs if c.key not in skip}


def get_facility(ctx: OpContext, ref: str) -> Facility:
    facility = ctx.session.execute(
        select(Facility).where(or_(Facility.id == ref, Facility.code == ref))
    ).scalar_one_or_none()
    if facility is None:
        raise NotFound(f"facility '{ref}' not found")
    ctx.bind_facility(facility.id)
    ctx.touch(facility.id)
    return facility


def get_entity(ctx: OpContext, model: type[T], entity_id: str, facility_id: str | None = None, label: str | None = None) -> T:
    obj = ctx.session.get(model, entity_id)
    if obj is None or (facility_id is not None and getattr(obj, "facility_id", facility_id) != facility_id):
        raise NotFound(f"{label or model.__name__} '{entity_id}' not found")
    if hasattr(obj, "facility_id") and getattr(obj, "facility_id"):
        ctx.bind_facility(obj.facility_id)
    ctx.touch(entity_id)
    return obj


def get_note_class(ctx: OpContext, facility: Facility, ref: str) -> NoteClass:
    nc = ctx.session.execute(
        select(NoteClass).where(NoteClass.facility_id == facility.id, or_(NoteClass.id == ref, NoteClass.name == ref))
    ).scalar_one_or_none()
    if nc is None:
        raise NotFound(f"note class '{ref}' not found in facility {facility.code}")
    ctx.touch(nc.id)
    return nc


def get_noteholder(ctx: OpContext, ref: str) -> Noteholder:
    nh = ctx.session.execute(
        select(Noteholder).where(or_(Noteholder.id == ref, Noteholder.short_name == ref))
    ).scalar_one_or_none()
    if nh is None:
        raise NotFound(f"noteholder '{ref}' not found")
    ctx.touch(nh.id)
    return nh


def note_classes(ctx: OpContext, facility_id: str) -> list[NoteClass]:
    return list(ctx.session.execute(
        select(NoteClass).where(NoteClass.facility_id == facility_id).order_by(NoteClass.rank)
    ).scalars())


def register_entries(ctx: OpContext, facility_id: str, as_of: date | None = None) -> list[RegisterEntry]:
    q = select(RegisterEntry).where(RegisterEntry.facility_id == facility_id)
    if as_of is not None:
        q = q.where(RegisterEntry.effective_date <= as_of)
    return list(ctx.session.execute(q.order_by(RegisterEntry.effective_date, RegisterEntry.created_at)).scalars())


def positions(ctx: OpContext, facility_id: str, as_of: date | None = None) -> dict[str, dict[str, dict[str, Decimal]]]:
    """{class_id: {holder_id: {"commitment": x, "outstanding": y}}} as at a date (inclusive)."""
    out: dict[str, dict[str, dict[str, Decimal]]] = defaultdict(lambda: defaultdict(lambda: {"commitment": ZERO, "outstanding": ZERO}))
    for e in register_entries(ctx, facility_id, as_of):
        pos = out[e.note_class_id][e.noteholder_id]
        if e.entry_type == "commitment":
            pos["commitment"] += e.amount
        else:
            pos["outstanding"] += e.amount * PRINCIPAL_SIGN[e.entry_type]
    return out


def class_outstanding(ctx: OpContext, facility_id: str, as_of: date | None = None) -> dict[str, Decimal]:
    pos = positions(ctx, facility_id, as_of)
    return {cid: sum((h["outstanding"] for h in holders.values()), ZERO) for cid, holders in pos.items()}


def movements_by_holder(ctx: OpContext, note_class_id: str) -> dict[str, list[Movement]]:
    rows = ctx.session.execute(
        select(RegisterEntry).where(RegisterEntry.note_class_id == note_class_id, RegisterEntry.entry_type != "commitment")
    ).scalars()
    out: dict[str, list[Movement]] = defaultdict(list)
    for e in rows:
        out[e.noteholder_id].append(Movement(e.effective_date, e.amount * PRINCIPAL_SIGN[e.entry_type]))
    return dict(out)
