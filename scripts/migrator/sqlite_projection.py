from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .rollout_scan import parse_rollout
from .version_guard import sqlite_guard


def rows_hash(rows: list[tuple[Any, ...]]) -> str:
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest().upper()


def session_state(db: Path, session_id: str, rollout: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        guard = sqlite_guard(connection)
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        if not guard["known"]:
            return {"schema_guard": guard, "quick_check": quick, "session_found": False, "scan_only_reason": "unknown SQLite schema"}
        projection = connection.execute("SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?", (session_id,)).fetchone()
        if projection is None:
            return {"schema_guard": guard, "quick_check": quick, "session_found": False}
        data, lines, _, starts, ends = parse_rollout(rollout)
        first = projection[1] - len(lines)
        start_map = {first + index: value for index, value in enumerate(starts)}
        end_map = {first + index: value for index, value in enumerate(ends)}
        turns = connection.execute("SELECT turn_id,rollout_ordinal,rollout_byte_offset,rollout_end_ordinal,rollout_end_byte_offset FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal", (session_id,)).fetchall()
        mismatches = []
        for turn_id, ordinal, byte_offset, end_ordinal, end_offset in turns:
            expected_start = start_map.get(ordinal)
            expected_end = end_map.get(end_ordinal) if end_ordinal is not None else None
            if byte_offset != expected_start or (end_ordinal is not None and end_offset != expected_end):
                mismatches.append({"kind": "update_offset", "turn_id": turn_id, "ordinal": ordinal, "start_old": byte_offset, "start_new": expected_start, "end_ordinal": end_ordinal, "end_old": end_offset, "end_new": expected_end})
        items = connection.execute("SELECT * FROM thread_items WHERE thread_id=? ORDER BY turn_id,item_id", (session_id,)).fetchall()
        realtime = connection.execute("SELECT * FROM thread_realtime_items WHERE thread_id=? ORDER BY rowid", (session_id,)).fetchall()
        return {
            "schema_guard": guard,
            "quick_check": quick,
            "session_found": True,
            "database": str(db.resolve()),
            "session_id": session_id,
            "projection": {"next_rollout_byte_offset": projection[0], "next_rollout_ordinal": projection[1], "at_eof": projection[0] == len(data), "ordinal_after_last": projection[1] == first + len(lines)},
            "first_ordinal": first,
            "turn_count": len(turns), "turns_hash": rows_hash(turns),
            "item_count": len(items), "items_hash": rows_hash(items),
            "realtime_count": len(realtime), "realtime_hash": rows_hash(realtime),
            "offset_mismatches": mismatches,
        }
    finally:
        connection.close()


def online_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(str(source))
    destination_connection = sqlite3.connect(str(destination))
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def repair_offsets(db: Path, session_id: str, repairs: list[dict[str, Any]]) -> int:
    connection = sqlite3.connect(str(db), timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed before repair")
        changed = 0
        for repair in repairs:
            cursor = connection.execute(
                "UPDATE thread_turns SET rollout_byte_offset=?,rollout_end_byte_offset=? WHERE thread_id=? AND turn_id=? AND rollout_byte_offset IS ? AND rollout_end_byte_offset IS ?",
                (repair["start_new"], repair["end_new"], session_id, repair["turn_id"], repair["start_old"], repair["end_old"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"guarded projection update failed for {repair['turn_id']}")
            changed += 1
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed after repair")
        connection.execute("COMMIT")
        return changed
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def rollback_offsets(db: Path, session_id: str, repairs: list[dict[str, Any]]) -> int:
    reversed_repairs = [
        {**repair, "start_old": repair["start_new"], "start_new": repair["start_old"], "end_old": repair["end_new"], "end_new": repair["end_old"]}
        for repair in repairs
    ]
    return repair_offsets(db, session_id, reversed_repairs)
