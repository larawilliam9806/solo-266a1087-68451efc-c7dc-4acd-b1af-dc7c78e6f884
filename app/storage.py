"""SQLite persistence for survey batches, households, and path answers."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS households (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    household_no TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    UNIQUE (batch_id, household_no)
);

CREATE TABLE IF NOT EXISTS answers (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    prefix TEXT NOT NULL,
    qid TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (household_id, prefix, qid)
);

-- One roster per batch.  The primary key rejects a second successful import.
CREATE TABLE IF NOT EXISTS batch_rosters (
    batch_id INTEGER PRIMARY KEY REFERENCES batches(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS roster_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    slot_no TEXT NOT NULL,
    position INTEGER NOT NULL,
    main_household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    UNIQUE (batch_id, slot_no),
    UNIQUE (batch_id, main_household_id)
);

CREATE TABLE IF NOT EXISTS roster_substitutes (
    slot_id INTEGER NOT NULL REFERENCES roster_slots(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    PRIMARY KEY (slot_id, household_id)
);

CREATE TABLE IF NOT EXISTS slot_locks (
    slot_id INTEGER PRIMARY KEY REFERENCES roster_slots(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    locked_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_roster_slots_batch
    ON roster_slots (batch_id, position);
CREATE INDEX IF NOT EXISTS idx_slot_locks_batch
    ON slot_locks (batch_id);
"""


def connect(path):
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    return connection


@contextmanager
def transaction(connection):
    """Serialise a write.  If the caller already owns a transaction, join it."""
    if connection.in_transaction:
        yield connection
        return
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        connection.execute("BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


@contextmanager
def read_transaction(connection):
    """A consistent read snapshot.

    The first SELECT fixes the snapshot; every roster, lock and answer read
    made inside the block therefore comes from the same committed state.  If
    the caller already owns a transaction this is a no-op.
    """
    if connection.in_transaction:
        yield connection
        return
    connection.execute("BEGIN")
    try:
        yield connection
    finally:
        connection.rollback()


def _prefix_key(prefix):
    return json.dumps(list(prefix), ensure_ascii=False, separators=(",", ":"))


def _prefix_tuple(key):
    try:
        value = json.loads(key)
    except (TypeError, ValueError):
        return ()
    return tuple(str(item) for item in value)


def create_batch(connection, name, definition, households, answers_by_household):
    """Insert a batch in one transaction.

    ``households`` is a list of ``(household_no, row_number)``.
    ``answers_by_household`` maps household_no to
    ``{(prefix_tuple, qid): value}``.
    """
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO batches (name, definition) VALUES (?, ?)",
            (name, json.dumps(definition, ensure_ascii=False)))
        batch_id = cursor.lastrowid
        for household_no, row_number in households:
            cursor = connection.execute(
                "INSERT INTO households (batch_id, household_no, row_number) "
                "VALUES (?, ?, ?)",
                (batch_id, household_no, row_number))
            household_id = cursor.lastrowid
            rows = [
                (household_id, _prefix_key(prefix), qid, value)
                for (prefix, qid), value in
                (answers_by_household.get(household_no) or {}).items()
            ]
            connection.executemany(
                "INSERT OR REPLACE INTO answers "
                "(household_id, prefix, qid, value) VALUES (?, ?, ?, ?)", rows)
    return batch_id


def get_batch(connection, batch_id):
    row = connection.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "definition": json.loads(row["definition"]),
        "created_at": row["created_at"],
    }


def list_batches(connection):
    rows = connection.execute(
        "SELECT b.id, b.name, b.created_at, COUNT(h.id) AS households "
        "FROM batches b LEFT JOIN households h ON h.batch_id = b.id "
        "GROUP BY b.id ORDER BY b.id DESC").fetchall()
    return [dict(row) for row in rows]


def list_households(connection, batch_id):
    rows = connection.execute(
        "SELECT id, household_no, row_number FROM households "
        "WHERE batch_id = ? ORDER BY row_number, id", (batch_id,)).fetchall()
    return [dict(row) for row in rows]


def household_index(connection, batch_id):
    return {row["household_no"]: row["id"]
            for row in list_households(connection, batch_id)}


def get_household(connection, batch_id, household_id):
    row = connection.execute(
        "SELECT id, household_no, row_number FROM households "
        "WHERE batch_id = ? AND id = ?",
        (batch_id, household_id)).fetchone()
    return dict(row) if row else None


def load_answers(connection, household_id):
    rows = connection.execute(
        "SELECT prefix, qid, value FROM answers WHERE household_id = ?",
        (household_id,)).fetchall()
    return {(_prefix_tuple(row["prefix"]), row["qid"]): row["value"]
            for row in rows}


def load_all_answers(connection, batch_id):
    """``{household_id: {(prefix, qid): value}}`` for one batch."""
    rows = connection.execute(
        "SELECT a.household_id, a.prefix, a.qid, a.value "
        "FROM answers a JOIN households h ON h.id = a.household_id "
        "WHERE h.batch_id = ?", (batch_id,)).fetchall()
    out = {}
    for row in rows:
        out.setdefault(row["household_id"], {})[
            (_prefix_tuple(row["prefix"]), row["qid"])] = row["value"]
    return out


def _write_answers(connection, household_id, changes):
    for (prefix, qid), value in changes.items():
        key = _prefix_key(prefix)
        if value is None:
            connection.execute(
                "DELETE FROM answers WHERE household_id = ? "
                "AND prefix = ? AND qid = ?",
                (household_id, key, qid))
        else:
            connection.execute(
                "INSERT INTO answers (household_id, prefix, qid, value) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (household_id, prefix, qid) "
                "DO UPDATE SET value = excluded.value",
                (household_id, key, qid, value))


def save_answers(connection, household_id, changes):
    """Persist ``{key: value_or_None}`` for one household.

    ``key`` is ``(prefix_tuple, qid)``; ``None`` deletes.  All changes are
    written in a single transaction, so a failure leaves the previous answers
    fully intact.  When the caller already opened a transaction the writes
    join it; the caller then controls the commit/rollback.
    """
    if connection.in_transaction:
        _write_answers(connection, household_id, changes)
        return
    with transaction(connection):
        _write_answers(connection, household_id, changes)


# ---------------------------------------------------------------------------
# Roster / sampling slots


def has_roster(connection, batch_id):
    row = connection.execute(
        "SELECT 1 FROM batch_rosters WHERE batch_id = ?", (batch_id,)).fetchone()
    return row is not None


def count_roster_slots(connection, batch_id):
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM roster_slots WHERE batch_id = ?",
        (batch_id,)).fetchone()
    return row["n"]


def create_roster(connection, batch_id, entries):
    """Insert the batch roster in one transaction.

    ``entries`` is an ordered list of
    ``{"slot_no": str, "main_id": int, "substitute_ids": [int, ...]}``.
    The ``batch_rosters`` primary key guarantees at most one roster per batch.
    """
    with transaction(connection):
        connection.execute(
            "INSERT INTO batch_rosters (batch_id) VALUES (?)", (batch_id,))
        for position, entry in enumerate(entries):
            cursor = connection.execute(
                "INSERT INTO roster_slots "
                "(batch_id, slot_no, position, main_household_id) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, entry["slot_no"], position, entry["main_id"]))
            slot_id = cursor.lastrowid
            connection.executemany(
                "INSERT INTO roster_substitutes "
                "(slot_id, position, household_id) VALUES (?, ?, ?)",
                [(slot_id, sub_position, household_id)
                 for sub_position, household_id in
                 enumerate(entry.get("substitute_ids") or [])])


def list_roster_slots(connection, batch_id, household_map=None):
    """Roster slots with their main/substitute households.

    ``household_map`` maps household id to a ``{id, household_no}`` dict; when
    omitted it is loaded from the batch.
    """
    if household_map is None:
        household_map = {row["id"]: row
                         for row in list_households(connection, batch_id)}
    rows = connection.execute(
        "SELECT id, slot_no, position, main_household_id "
        "FROM roster_slots WHERE batch_id = ? "
        "ORDER BY position, id", (batch_id,)).fetchall()
    slots = []
    by_id = {}
    for row in rows:
        slot = {
            "id": row["id"],
            "slot_no": row["slot_no"],
            "position": row["position"],
            "main": _household_ref(household_map, row["main_household_id"]),
            "substitutes": [],
        }
        slots.append(slot)
        by_id[slot["id"]] = slot

    sub_rows = connection.execute(
        "SELECT rs.slot_id, rs.household_id "
        "FROM roster_substitutes rs "
        "JOIN roster_slots s ON s.id = rs.slot_id "
        "WHERE s.batch_id = ? ORDER BY rs.slot_id, rs.position",
        (batch_id,)).fetchall()
    for row in sub_rows:
        slot = by_id.get(row["slot_id"])
        if slot is not None:
            slot["substitutes"].append(
                _household_ref(household_map, row["household_id"]))
    return slots


def _household_ref(household_map, household_id):
    row = household_map.get(household_id)
    if row is None:
        return {"id": household_id, "household_no": str(household_id)}
    return {"id": row["id"], "household_no": row["household_no"],
            "row_number": row.get("row_number")}


def get_locks(connection, batch_id):
    rows = connection.execute(
        "SELECT slot_id, household_id FROM slot_locks WHERE batch_id = ?",
        (batch_id,)).fetchall()
    return {row["slot_id"]: row["household_id"] for row in rows}


def lock_for_household(connection, batch_id, household_id):
    row = connection.execute(
        "SELECT l.slot_id, l.household_id, s.slot_no "
        "FROM slot_locks l JOIN roster_slots s ON s.id = l.slot_id "
        "WHERE l.batch_id = ? AND l.household_id = ? "
        "ORDER BY s.position, s.id LIMIT 1",
        (batch_id, household_id)).fetchone()
    return dict(row) if row else None


def set_lock(connection, batch_id, slot_id, household_id):
    connection.execute(
        "INSERT INTO slot_locks (slot_id, batch_id, household_id) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT (slot_id) DO UPDATE SET "
        "household_id = excluded.household_id, batch_id = excluded.batch_id, "
        "locked_at = datetime('now', 'localtime')",
        (slot_id, batch_id, household_id))


def delete_lock(connection, batch_id, slot_id):
    cursor = connection.execute(
        "DELETE FROM slot_locks WHERE batch_id = ? AND slot_id = ?",
        (batch_id, slot_id))
    return cursor.rowcount > 0


def load_snapshot(connection, batch_id):
    """Roster, lock state and every household's answers from one snapshot.

    List views and CSV downloads build their result exclusively from this
    dict, so the three parts can never come from different saved states.
    """
    with read_transaction(connection):
        return _load_snapshot(connection, batch_id)


def _load_snapshot(connection, batch_id):
    info = get_batch(connection, batch_id)
    if info is None:
        return None
    households = list_households(connection, batch_id)
    household_map = {row["id"]: row for row in households}
    return {
        "batch": info,
        "households": households,
        "household_map": household_map,
        "slots": list_roster_slots(connection, batch_id, household_map),
        "locks": get_locks(connection, batch_id),
        "answers": load_all_answers(connection, batch_id),
        "has_roster": has_roster(connection, batch_id),
    }


def db_path_from_env():
    return os.environ.get("SURVEY_DB", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "survey.sqlite3"))
