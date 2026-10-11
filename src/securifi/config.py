"""Runtime configuration.

All configuration is read from environment variables so the same image can be
deployed into any environment (12-factor). Securifi is single tenant: one
deployment serves one business, against its own database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./var/securifi.db"
    # Name of the business operating this deployment; shown in the UI and reports.
    organisation_name: str = "Securifi"
    environment: str = "development"
    # Allowed CORS origins for the API (comma separated). Empty means same-origin only.
    cors_origins: list[str] = field(default_factory=list)
    # Expose the MCP streamable HTTP endpoint at /mcp on the API server.
    mcp_http_enabled: bool = True
    # Write SQL to the log (debugging only).
    sql_echo: bool = False
    # Fail closed: refuse to start in production on SQLite.
    allow_sqlite_in_production: bool = False
    # Create missing tables at start-up (convenient for dev / single-node installs).
    auto_create_schema: bool = True

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"}


@lru_cache
def get_settings() -> Settings:
    origins = os.environ.get("SECURIFI_CORS_ORIGINS", "")
    return Settings(
        database_url=os.environ.get("SECURIFI_DATABASE_URL", Settings.database_url),
        organisation_name=os.environ.get("SECURIFI_ORGANISATION_NAME", Settings.organisation_name),
        environment=os.environ.get("SECURIFI_ENVIRONMENT", Settings.environment),
        cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
        mcp_http_enabled=_bool(os.environ.get("SECURIFI_MCP_HTTP_ENABLED"), True),
        sql_echo=_bool(os.environ.get("SECURIFI_SQL_ECHO"), False),
        allow_sqlite_in_production=_bool(os.environ.get("SECURIFI_ALLOW_SQLITE_IN_PRODUCTION"), False),
        auto_create_schema=_bool(os.environ.get("SECURIFI_AUTO_CREATE_SCHEMA"), True),
    )
