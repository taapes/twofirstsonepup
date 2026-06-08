"""Whole-DB snapshot / restore for safe testing.

Take a snapshot of the current (good) state, then test freely on the site —
fake drafts, fake trades, etc. — and restore to revert exactly to the snapshot.

  python snapshot.py save [path]       # default: snapshots/<timestamp>.json
  python snapshot.py restore <path>    # wipe app tables + reload from the file
  python snapshot.py list

Snapshots are written under snapshots/ (gitignored). Restore replaces ALL app
tables (alembic_version is left alone), so it's a full point-in-time revert.
"""

import datetime
import glob
import json
import os
import sys
import uuid

from sqlalchemy import Date, DateTime, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

import models  # noqa: F401 — registers all tables on Base.metadata
from db import Base, engine

SNAP_DIR = "snapshots"


def _ser(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return v


def _coerce(col, v):
    if v is None:
        return None
    t = col.type
    if isinstance(t, PG_UUID):
        return uuid.UUID(v)
    if isinstance(t, DateTime):
        return datetime.datetime.fromisoformat(v)
    if isinstance(t, Date):
        return datetime.date.fromisoformat(v)
    return v


def save(path: str) -> None:
    data = {}
    with engine.connect() as c:
        for t in Base.metadata.sorted_tables:
            data[t.name] = [
                {k: _ser(v) for k, v in r._mapping.items()} for r in c.execute(select(t))
            ]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)
    print(f"saved {sum(len(v) for v in data.values())} rows across "
          f"{len(data)} tables -> {path}")


def restore(path: str) -> None:
    data = json.load(open(path))
    with engine.begin() as c:
        for t in reversed(Base.metadata.sorted_tables):  # children first
            c.execute(t.delete())
        for t in Base.metadata.sorted_tables:  # parents first
            rows = data.get(t.name, [])
            if rows:
                c.execute(t.insert(), [
                    {k: _coerce(t.c[k], v) for k, v in row.items()} for row in rows
                ])
    print(f"restored {sum(len(v) for v in data.values())} rows from {path}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "save"
    if cmd == "save":
        path = sys.argv[2] if len(sys.argv) > 2 else (
            f"{SNAP_DIR}/{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
        )
        save(path)
    elif cmd == "restore":
        restore(sys.argv[2])
    elif cmd == "list":
        for p in sorted(glob.glob(f"{SNAP_DIR}/*.json")):
            print(p)
    else:
        print(__doc__)
