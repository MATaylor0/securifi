"""The single registry of operations.

Every capability of the platform is declared once here, with a typed input model, the
permission it needs and whether it changes state. The UI, REST API, MCP server and CLI
are generated from this registry and all execute through ``executor.execute`` - so
permissions, validation, transactions and audit logging are identical everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict


class OpInput(BaseModel):
    """Base for operation inputs: unknown fields are rejected so typos never pass silently."""

    model_config = ConfigDict(extra="forbid")


class NoInput(OpInput):
    pass


Handler = Callable[[Any, Any], Any]


@dataclass(frozen=True)
class Operation:
    name: str
    summary: str
    permission: str
    input_model: type[OpInput]
    handler: Handler
    mutating: bool
    tags: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""

    @property
    def tool_name(self) -> str:
        """Name used for MCP tools (dots are not universally supported by clients)."""
        return self.name.replace(".", "_")

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "description": self.description,
            "permission": self.permission,
            "mutating": self.mutating,
            "tags": list(self.tags),
            "input_schema": self.input_schema(),
        }


_REGISTRY: dict[str, Operation] = {}


def operation(
    name: str,
    *,
    summary: str,
    permission: str,
    input: type[OpInput] = NoInput,
    mutating: bool,
    tags: tuple[str, ...] = (),
) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        if name in _REGISTRY:
            raise ValueError(f"operation '{name}' registered twice")
        from .permissions import PERMISSIONS

        if permission not in PERMISSIONS:
            raise ValueError(f"operation '{name}' uses unknown permission '{permission}'")
        _REGISTRY[name] = Operation(
            name=name, summary=summary, permission=permission, input_model=input, handler=fn,
            mutating=mutating, tags=tags or (name.split(".")[0],), description=(fn.__doc__ or "").strip(),
        )
        return fn

    return decorator


def load_operations() -> dict[str, Operation]:
    """Import all handler modules (idempotent) and return the registry."""
    from . import handlers  # noqa: F401

    handlers.load_all()
    return _REGISTRY


def get_operation(name: str) -> Operation | None:
    load_operations()
    return _REGISTRY.get(name) or next((op for op in _REGISTRY.values() if op.tool_name == name), None)


def all_operations() -> list[Operation]:
    return sorted(load_operations().values(), key=lambda op: op.name)
