"""Test-database isolation.

Most of this suite runs against whatever DATABASE_URL points at — historically the
live Neon database. That is tolerable for read-only assertions, but NOT for tests
that exercise sync_players: its first phase NULLs fpl_id across the pool and the
second rewrites identity, committed, with no rollback. Running that against prod
would reproduce the exact incident the code under test exists to prevent.

So any test that calls into `sync` must depend on `test_session`, which points the
app at TEST_DATABASE_URL and refuses to run if that is unset or equal to
DATABASE_URL.

Bring one up locally with:

    docker run -d --name fpl-test-pg -e POSTGRES_PASSWORD=test \\
        -e POSTGRES_DB=fpltest -p 55432:5432 postgres:16
    export TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/fpltest

SQLite is not an option: models.py uses postgresql JSONB/UUID, and the partial
unique index the sync phases rely on is Postgres-only.
"""

import os
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


@pytest.fixture(scope="session")
def test_engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL not set (Neon branch or local postgres); "
            "refusing to run sync tests against the configured database"
        )
    if url == os.getenv("DATABASE_URL"):
        pytest.fail("TEST_DATABASE_URL must differ from DATABASE_URL")

    # Schema comes from Alembic, not Base.metadata.create_all: the partial unique
    # index uq_players_fpl_id_live exists only in the migration, and the id-swap
    # test passes vacuously without it.
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(
        ["alembic", "upgrade", "head"],
        check=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True,
    )
    return create_engine(url, future=True)


@pytest.fixture
def test_session(test_engine, monkeypatch):
    Sess = sessionmaker(
        bind=test_engine, autoflush=False, expire_on_commit=False, future=True
    )
    import db as _db
    import sync as _sync

    monkeypatch.setattr(_db, "SessionLocal", Sess)
    # Mandatory: sync.py did `from db import SessionLocal`, so it holds its own
    # reference and patching db.SessionLocal alone would leave it on the real DB.
    monkeypatch.setattr(_sync, "SessionLocal", Sess)

    s = Sess()
    try:
        yield s
    finally:
        s.close()
        with test_engine.begin() as c:
            names = [
                r[0]
                for r in c.execute(text(
                    "select tablename from pg_tables where schemaname='public' "
                    "and tablename <> 'alembic_version'"
                ))
            ]
            if names:
                c.exec_driver_sql(
                    "TRUNCATE TABLE "
                    + ", ".join(f'"{n}"' for n in names)
                    + " RESTART IDENTITY CASCADE"
                )
