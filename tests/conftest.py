import os

import pytest

from securifi import db
from securifi.ops.auth import issue_key
from securifi.models import User


@pytest.fixture()
def database(tmp_path):
    """SQLite by default; set SECURIFI_TEST_DATABASE_URL to run the suite against PostgreSQL."""
    url = os.environ.get("SECURIFI_TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/test.db"
    db.configure(url)
    if not url.startswith("sqlite"):
        db.Base.metadata.drop_all(db.get_engine())
    db.create_schema()
    yield url
    db.get_engine().dispose()


@pytest.fixture()
def users(database):
    """Three users with API keys: admin, trust manager (approver) and analyst (preparer)."""
    out = {}
    with db.session_scope() as s:
        for role in ("admin", "trust_manager", "analyst", "agent", "viewer"):
            u = User(email=f"{role}@example.com", name=role.title(), role=role)
            s.add(u)
            s.flush()
            _, raw = issue_key(s, u, f"{role} key")
            out[role] = raw
    return out
