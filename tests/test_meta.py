"""Guards on the test suite itself.

REPLACES conftest's COMMITTING_DB_MODULES refusal, removed 2026-08-31 once the three
files it named were converted onto `test_session`.

That list could have been kept with an empty set, but a guard with no members is a
guard that can never fire — the silent-inert shape this repo keeps finding, and a
permanent invitation to add a fourth file that nobody notices. This asserts the actual
mistake instead: importing the sessionmaker by name.
"""

import pathlib
import re

TESTS_DIR = pathlib.Path(__file__).parent

# `from db import SessionLocal` (or `import db.SessionLocal as ...`) binds a COPY of the
# sessionmaker into the importing module's namespace. conftest's `test_session` patches
# the attribute on the `db` module, so it can never reach that copy — the module keeps
# talking to whatever DATABASE_URL points at, which on any dev machine is production
# Neon (db.py calls load_dotenv()). Three test files did exactly this and were skipped
# by default for a year to contain the damage; two of them wrote to production on
# 2026-08-18.
#
# sync.py has the same binding, which is why conftest patches `sync.SessionLocal` too —
# see the comment there. That is a deliberate, contained exception in APPLICATION code;
# it must not spread into tests.
_BAD_IMPORT = re.compile(
    r"^\s*(from\s+db\s+import\s+[^\n]*\bSessionLocal\b"
    r"|import\s+db\.SessionLocal\b)",
    re.MULTILINE,
)


def test_no_test_module_imports_sessionlocal_from_db():
    """A test that opens its own SessionLocal talks to DATABASE_URL — production.

    Use the `test_session` fixture. It patches `db.SessionLocal` AND
    `sync.SessionLocal`, so code under test that opens its own session (every sync task
    does) still lands in the test database, and its teardown truncates.
    """
    offenders = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if _BAD_IMPORT.search(path.read_text()):
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} import SessionLocal from db, which resolves to DATABASE_URL "
        "(production on a dev machine) and cannot be redirected by the test_session "
        "fixture. Take `test_session` and seed what the test needs instead."
    )


def test_the_guard_would_actually_catch_it(tmp_path):
    """Pins the regex, so a rename or a reformat can't quietly disarm the check above.

    Without this the guard degrades to 'no file matched a pattern that matches nothing'
    — which is exactly the failure mode it exists to prevent.
    """
    assert _BAD_IMPORT.search("from db import SessionLocal\n")
    assert _BAD_IMPORT.search("from db import Base, SessionLocal\n")
    assert _BAD_IMPORT.search("    from db import SessionLocal\n"), "indented too"
    # Things that are fine and must not trip it.
    assert not _BAD_IMPORT.search("import db\n")
    assert not _BAD_IMPORT.search("from models import League\n")
    assert not _BAD_IMPORT.search("# from db import SessionLocal (don't)\n")
