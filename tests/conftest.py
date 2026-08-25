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

OPT-OUT: set ALLOW_DB_SKIP=1 to allow tests to run without TEST_DATABASE_URL. This
is safe for pure-rules tests (no database dependency), but DB-backed tests will
silently skip, producing misleading "green" runs. Use only when deliberately running
test suites that don't touch the database.
"""

import os
import shutil
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def pytest_configure(config):
    """Fail the whole run, before collection, rather than let it read as green.

    Two failure modes are checked here and not in the fixture, for the same reason:
    a session-scoped fixture that raises has its exception CACHED and re-raised for
    every test that depends on it, so one broken precondition renders as hundreds of
    identical errors with the real cause buried. Checked at configure time it renders
    once, as `ERROR: <message>`, before a single test is collected.

    `pytest.UsageError` is deliberate over a bare raise: an exception escaping this
    hook becomes an INTERNALERROR traceback through pluggy's frames, which is loud
    but buries the instruction the reader actually needs.
    """
    url = os.getenv("TEST_DATABASE_URL")

    # Trap 1: a silent skip reads as success. "243 passed, 310 skipped, exit 0" has
    # already produced false-green regressions here, so an unset TEST_DATABASE_URL
    # must stop the run unless the caller says, explicitly, that they meant it.
    if not url:
        if not os.getenv("ALLOW_DB_SKIP"):
            raise pytest.UsageError(
                "TEST_DATABASE_URL is not set, so every DB-backed test would skip "
                "and the run would exit 0 — a false green.\n\n"
                "  Full regression (0 skipped):\n"
                "    docker run -d --name fpl-test-pg -e POSTGRES_PASSWORD=test \\\n"
                "        -e POSTGRES_DB=fpltest -p 55432:5432 postgres:16\n"
                "    export TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/fpltest\n\n"
                "  Pure-rules tests only (DB tests skip, on purpose):\n"
                "    ALLOW_DB_SKIP=1 pytest"
            )
        return

    # Trap 2: the schema is built by shelling out to `alembic` (see test_engine), so
    # an unactivated venv means alembic is missing. Resolved here because the fixture
    # would otherwise report the same FileNotFoundError once per dependent test.
    if shutil.which("alembic") is None:
        raise pytest.UsageError(
            "alembic is not on PATH, but TEST_DATABASE_URL is set — the test schema "
            "is built from migrations, not from Base.metadata.create_all, so the run "
            "cannot proceed.\n\n"
            "  Activate the venv:  source .venv/bin/activate\n"
            "  ...or prefix PATH:  PATH=\"$PWD/.venv/bin:$PATH\" pytest"
        )


# Test modules that build their sessions from db.SessionLocal() — i.e. from
# DATABASE_URL — and COMMIT. That is deliberate and correct: they cover code that
# commits internally (services.record_audit, the sync.* tasks), which a rollback-based
# fixture cannot test. The defect is that nothing about DATABASE_URL says "test", and
# db.py calls load_dotenv(), so on any dev machine with a normal .env they connect to
# PRODUCTION Neon and commit there.
#
# The convention used to be "remember to pass --ignore for these three". On 2026-08-24
# that convention failed on a shell detail (zsh does not word-split an unquoted "$IG",
# so all three flags arrived as one unmatched path and pytest dropped them silently)
# and they ran against production twice. A guard that needs a correctly hand-typed flag
# every time is not a guard, so the default is now inverted: they are refused unless
# the caller has deliberately pointed them somewhere safe.
COMMITTING_DB_MODULES = frozenset(
    {"test_audit.py", "test_demo.py", "test_sync_freeze.py"}
)


def _committing_tests_refusal():
    """Return a refusal reason, or None if the caller has sanctioned these tests.

    ONE sanction only, and deliberately not "DATABASE_URL == TEST_DATABASE_URL":
    `test_engine` above asserts the two must DIFFER, so equality would trade a prod
    write for a failure in every other DB-backed test. Anyone who wants these to run
    must say so outright, ideally with DATABASE_URL pointed at a scratch database.
    """
    if os.getenv("ALLOW_COMMITTING_DB_TESTS"):
        return None

    db_url = os.getenv("DATABASE_URL")
    # Host only — never echo credentials into test output.
    where = "an unset DATABASE_URL"
    if db_url:
        where = db_url.split("@")[-1].split("/")[0] if "@" in db_url else db_url
    return (
        f"refused: this module COMMITS to DATABASE_URL ({where}). Set "
        "ALLOW_COMMITTING_DB_TESTS=1 to allow it, and point DATABASE_URL at a "
        "scratch database first unless you mean to write there."
    )


def pytest_collection_modifyitems(config, items):
    """Refuse the committing modules by default, so a plain `pytest` cannot reach prod.

    A skip (not a deselect) on purpose: these must stay VISIBLE in the summary. The
    whole point of this file is that invisible skips get misread as coverage.
    """
    reason = _committing_tests_refusal()
    if reason is None:
        return

    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if os.path.basename(str(item.path)) in COMMITTING_DB_MODULES:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def test_engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        # Only reachable with ALLOW_DB_SKIP=1 — pytest_configure stops the run
        # otherwise, so these skips are always a choice someone made out loud.
        pytest.skip("TEST_DATABASE_URL not set; ALLOW_DB_SKIP=1 opted out of DB tests")
    if url == os.getenv("DATABASE_URL"):
        pytest.fail("TEST_DATABASE_URL must differ from DATABASE_URL")

    # Schema comes from Alembic, not Base.metadata.create_all: the partial unique
    # index uq_players_fpl_id_live exists only in the migration, and the id-swap
    # test passes vacuously without it.
    env = {**os.environ, "DATABASE_URL": url}
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        subprocess.run(
            ["alembic", "upgrade", "head"],
            check=True,
            env=env,
            cwd=repo_root,
            capture_output=True,
        )
    except FileNotFoundError as e:
        # pytest_configure resolves alembic before collection, so this is only
        # reachable if it vanished mid-run. Kept so the cause stays named.
        raise RuntimeError(
            "alembic disappeared from PATH mid-run; the test schema is built "
            "from migrations. Activate the venv and re-run."
        ) from e

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
