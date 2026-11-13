import uuid
from pathlib import Path

import psycopg
import pytest

from solution.db import connect, init_schema
from solution.pii import Pii

TEST_KEY = b"test-master-key"

# pytest.ini puts pytest's temp dir at .pytest_cache/tmp; on a fresh clone the parent does not exist yet
(Path(__file__).resolve().parents[2] / ".pytest_cache").mkdir(exist_ok=True)


@pytest.fixture
def db():
    """A throw-away Postgres schema per test; skipped if Postgres is down (`docker compose up -d`)."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    try:
        conn = connect(schema=name)
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not reachable ({e.__class__.__name__}); run `docker compose up -d`")
    init_schema(conn, Pii(TEST_KEY))
    yield conn
    conn.execute(f'DROP SCHEMA "{name}" CASCADE')
    conn.close()


@pytest.fixture
def make_db():
    """Factory for several independent throw-away databases in one test."""
    made = []

    def mk():
        name = f"t_{uuid.uuid4().hex[:12]}"
        try:
            conn = connect(schema=name)
        except psycopg.OperationalError as e:
            pytest.skip(f"Postgres not reachable ({e.__class__.__name__}); run `docker compose up -d`")
        init_schema(conn, Pii(TEST_KEY))
        made.append((name, conn))
        return conn

    yield mk
    for name, conn in made:
        conn.execute(f'DROP SCHEMA "{name}" CASCADE')
        conn.close()
