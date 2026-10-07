from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .models import canonical_sha256, file_state, read_json, write_json
from .sqlite_projection import online_backup, session_state


STATE_TABLE_INFO = [
    ("id", "TEXT", 0, None, 1), ("rollout_path", "TEXT", 1, None, 0),
    ("created_at", "INTEGER", 1, None, 0), ("updated_at", "INTEGER", 1, None, 0),
    ("source", "TEXT", 1, None, 0), ("model_provider", "TEXT", 1, None, 0),
    ("cwd", "TEXT", 1, None, 0), ("title", "TEXT", 1, None, 0),
    ("sandbox_policy", "TEXT", 1, None, 0), ("approval_mode", "TEXT", 1, None, 0),
    ("tokens_used", "INTEGER", 1, "0", 0), ("has_user_event", "INTEGER", 1, "0", 0),
    ("archived", "INTEGER", 1, "0", 0), ("archived_at", "INTEGER", 0, None, 0),
    ("git_sha", "TEXT", 0, None, 0), ("git_branch", "TEXT", 0, None, 0),
    ("git_origin_url", "TEXT", 0, None, 0), ("cli_version", "TEXT", 1, "''", 0),
    ("first_user_message", "TEXT", 1, "''", 0), ("agent_nickname", "TEXT", 0, None, 0),
    ("agent_role", "TEXT", 0, None, 0), ("memory_mode", "TEXT", 1, "'enabled'", 0),
    ("model", "TEXT", 0, None, 0), ("reasoning_effort", "TEXT", 0, None, 0),
    ("agent_path", "TEXT", 0, None, 0), ("created_at_ms", "INTEGER", 0, None, 0),
    ("updated_at_ms", "INTEGER", 0, None, 0), ("thread_source", "TEXT", 0, None, 0),
    ("preview", "TEXT", 1, "''", 0), ("recency_at", "INTEGER", 1, "0", 0),
    ("recency_at_ms", "INTEGER", 1, "0", 0), ("history_mode", "TEXT", 1, "'legacy'", 0),
    ("name", "TEXT", 0, None, 0), ("is_pinned", "INTEGER", 1, "0", 0),
    ("thread_section_id", "TEXT", 0, None, 0), ("section_position", "INTEGER", 0, None, 0),
    ("section_entered_at_ms", "INTEGER", 0, None, 0), ("project_id", "TEXT", 0, None, 0),
    ("originator", "TEXT", 0, None, 0), ("daybreak_enabled", "BOOLEAN", 0, None, 0),
    ("creator_user_id", "TEXT", 0, None, 0), ("creator_account_id", "TEXT", 0, None, 0),
]
CATALOG_TABLE_INFO = [
    ("host_id", "TEXT", 1, None, 1), ("thread_id", "TEXT", 1, None, 2),
    ("display_title", "TEXT", 1, None, 0), ("source_created_at", "REAL", 1, None, 0),
    ("source_updated_at", "REAL", 1, None, 0), ("cwd", "TEXT", 0, None, 0),
    ("source_kind", "TEXT", 1, None, 0), ("source_detail", "TEXT", 0, None, 0),
    ("model_provider", "TEXT", 0, None, 0), ("git_branch", "TEXT", 0, None, 0),
    ("observation_sequence", "INTEGER", 1, None, 0), ("missing_candidate", "INTEGER", 1, "0", 0),
    ("thread_source", "TEXT", 0, None, 0), ("source_recency_at", "REAL", 1, "0", 0),
    ("pending_observed_title", "INTEGER", 1, "0", 0), ("project_id", "TEXT", 0, None, 0),
    ("conversation_origin", "TEXT", 0, None, 0), ("trial_conversation_type", "TEXT", 0, None, 0),
    ("chatgpt_async_status", "INTEGER", 0, None, 0),
]


def _table_info(connection: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    return [tuple(row[1:6]) for row in connection.execute(f"PRAGMA table_info({table})")]


def _schema_guard(connection: sqlite3.Connection, table: str, expected: list[tuple[Any, ...]]) -> dict[str, Any]:
    observed = _table_info(connection, table)
    return {"known": observed == expected, "table": table, "fingerprint": canonical_sha256(observed), "observed": [list(item) for item in observed], "expected_fingerprint": canonical_sha256(expected)}


def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def scan_bootstrap_metadata(state_db: Path, catalog_db: Path, thread_id: str, host_id: str = "local") -> dict[str, Any]:
    result: dict[str, Any] = {"thread_id": thread_id, "host_id": host_id}
    for name, db, table, expected, query, params, fields in (
        ("state", state_db, "threads", STATE_TABLE_INFO,
         "SELECT id,model_provider,model,reasoning_effort,rollout_path,cli_version FROM threads WHERE id=?", (thread_id,),
         ["id", "model_provider", "model", "reasoning_effort", "rollout_path", "cli_version"]),
        ("catalog", catalog_db, "local_thread_catalog", CATALOG_TABLE_INFO,
         "SELECT host_id,thread_id,model_provider,cwd,source_kind,source_detail FROM local_thread_catalog WHERE host_id=? AND thread_id=?", (host_id, thread_id),
         ["host_id", "thread_id", "model_provider", "cwd", "source_kind", "source_detail"]),
    ):
        connection = _open_ro(db)
        try:
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            guard = _schema_guard(connection, table, expected)
            rows = connection.execute(query, params).fetchall() if guard["known"] else []
            row = dict(zip(fields, rows[0])) if len(rows) == 1 else None
            result[name] = {"database": str(db.resolve()), "table": table, "quick_check": quick, "schema_guard": guard, "row_count": len(rows), "row": row, "row_hash": canonical_sha256(row) if row is not None else None}
        finally:
            connection.close()
    result["write_allowed"] = all(result[key]["quick_check"] == "ok" and result[key]["schema_guard"]["known"] and result[key]["row_count"] == 1 for key in ("state", "catalog"))
    return result


def create_bootstrap_plan(scan_path: Path, target_path: Path) -> dict[str, Any]:
    scan = read_json(scan_path)
    target = read_json(target_path)
    bootstrap = scan.get("bootstrap_state")
    if not scan.get("write_allowed") or not bootstrap or not bootstrap.get("write_allowed"):
        raise ValueError("scan guards are not write-safe for bootstrap metadata")
    if scan.get("candidate", {}).get("selection_status", "confirmed") != "confirmed":
        raise ValueError("candidate-only cwd matches cannot enter a migration plan")
    if set(target) != {"provider", "model", "reasoning"} or not all(isinstance(value, str) and value for value in target.values()):
        raise ValueError("target must contain provider, model, and reasoning")
    state = bootstrap["state"]["row"]
    catalog = bootstrap["catalog"]["row"]
    operations = []
    state_target = {"model_provider": target["provider"], "model": target["model"], "reasoning_effort": target["reasoning"]}
    state_changes = {key: value for key, value in state_target.items() if state[key] != value}
    if state_changes:
        operations.append({"database": "state", "database_path": bootstrap["state"]["database"], "table": "threads", "key": {"id": bootstrap["thread_id"]}, "expected": {"model_provider": state["model_provider"], "model": state["model"], "reasoning_effort": state["reasoning_effort"]}, "set": state_changes})
    if catalog["model_provider"] != target["provider"]:
        operations.append({"database": "catalog", "database_path": bootstrap["catalog"]["database"], "table": "local_thread_catalog", "key": {"host_id": bootstrap["host_id"], "thread_id": bootstrap["thread_id"]}, "expected": {"model_provider": catalog["model_provider"]}, "set": {"model_provider": target["provider"]}})
    if not operations:
        raise ValueError("bootstrap metadata already matches target")
    plan = {"format_version": 1, "stage": "thread-bootstrap-metadata", "codex_guard": scan["codex_guard"], "source_pre": scan["rollout_scan"]["rollout"], "sqlite_projection_pre": scan["sqlite_state"], "bootstrap_pre": bootstrap, "migration_target": target, "operations": operations}
    plan["plan_sha256"] = canonical_sha256(plan)
    return plan


def _verify_plan(plan: dict[str, Any]) -> None:
    expected = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if plan.get("stage") != "thread-bootstrap-metadata" or not expected or canonical_sha256(unsigned) != expected:
        raise ValueError("bootstrap plan SHA-256 is invalid")


def _guarded_update(connection: sqlite3.Connection, table: str, key: dict[str, Any], expected: dict[str, Any], values: dict[str, Any]) -> int:
    set_sql = ",".join(f"{name}=?" for name in values)
    clauses = [f"{name} IS ?" for name in (*key, *expected)]
    parameters = [*values.values(), *key.values(), *expected.values()]
    cursor = connection.execute(f"UPDATE {table} SET {set_sql} WHERE {' AND '.join(clauses)}", parameters)
    if cursor.rowcount != 1:
        raise RuntimeError(f"guarded update row count must equal 1, got {cursor.rowcount}")
    return cursor.rowcount


def _apply_one(db: Path, operation: dict[str, Any]) -> None:
    connection = sqlite3.connect(str(db), timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed before update")
        _guarded_update(connection, operation["table"], operation["key"], operation["expected"], operation["set"])
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed after update")
        connection.execute("COMMIT")
    except Exception:
        try: connection.execute("ROLLBACK")
        except sqlite3.Error: pass
        raise
    finally:
        connection.close()


def _reverse(operation: dict[str, Any]) -> dict[str, Any]:
    return {**operation, "expected": operation["set"], "set": {key: operation["expected"][key] for key in operation["set"]}}


def apply_bootstrap_plan(plan: dict[str, Any], confirmation_sha: str, output_dir: Path, failure_hook: Callable[[str], None] | None = None) -> dict[str, Any]:
    _verify_plan(plan)
    if confirmation_sha.upper() != plan["plan_sha256"]:
        raise ValueError("confirmation SHA does not match plan")
    if not plan.get("codex_guard", {}).get("known"):
        raise ValueError("unknown Codex version is scan-only")
    rollout = Path(plan["source_pre"]["path"])
    if file_state(rollout, plan["source_pre"].get("last_ordinal")) != plan["source_pre"]:
        raise ValueError("rollout state changed; plan is stale")
    projection_pre = plan["sqlite_projection_pre"]
    if session_state(Path(projection_pre["database"]), projection_pre["session_id"], rollout) != projection_pre:
        raise ValueError("SQLite projection state changed; plan is stale")
    pre = plan["bootstrap_pre"]
    state_db, catalog_db = Path(pre["state"]["database"]), Path(pre["catalog"]["database"])
    current = scan_bootstrap_metadata(state_db, catalog_db, pre["thread_id"], pre["host_id"])
    if current != pre:
        raise ValueError("bootstrap metadata state changed; plan is stale")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backups = {}
    for name, db in (("state", state_db), ("catalog", catalog_db)):
        destination = output_dir / "backups" / f"{db.name}.{stamp}.bak"
        online_backup(db, destination)
        backup_connection = sqlite3.connect(str(destination))
        try: check = backup_connection.execute("PRAGMA quick_check").fetchone()[0]
        finally: backup_connection.close()
        if check != "ok": raise RuntimeError(f"{name} online backup failed validation")
        backups[name] = {"path": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest().upper()}
    journal_changes = []
    for operation in plan["operations"]:
        source = pre[operation["database"]]
        journal_changes.append({**operation, "schema_hash": source["schema_guard"]["fingerprint"], "row_hash": source["row_hash"], "old_value": operation["expected"], "new_value": operation["set"], "reverse_guard": {"expected": operation["set"], "set": {key: operation["expected"][key] for key in operation["set"]}}})
    journal = {"format_version": 1, "stage": "thread-bootstrap-metadata", "status": "prepared", "plan_sha256": plan["plan_sha256"], "backups": backups, "changes": journal_changes}
    journal_path = output_dir / "bootstrap-change-journal.json"
    write_json(journal_path, journal)
    state_operation = next((item for item in plan["operations"] if item["database"] == "state"), None)
    catalog_operation = next((item for item in plan["operations"] if item["database"] == "catalog"), None)
    try:
        if failure_hook: failure_hook("before-state")
        if state_operation: _apply_one(state_db, state_operation)
        journal["status"] = "state-committed"; write_json(journal_path, journal)
        if failure_hook: failure_hook("before-catalog")
        if catalog_operation: _apply_one(catalog_db, catalog_operation)
        journal["status"] = "committed"; write_json(journal_path, journal)
    except Exception as error:
        if state_operation and journal["status"] == "state-committed":
            try:
                if failure_hook: failure_hook("before-compensation")
                _apply_one(state_db, _reverse(state_operation))
                journal["status"] = "compensated"
                journal["failure"] = str(error)
                write_json(journal_path, journal)
            except Exception as compensation_error:
                journal["status"] = "high-risk-compensation-failed"
                journal["failure"] = str(error); journal["compensation_failure"] = str(compensation_error)
                write_json(journal_path, journal)
                raise RuntimeError(f"HIGH-RISK: compensation rollback failed; preserve journal and backups for disaster recovery: {journal_path}") from compensation_error
        raise
    after = scan_bootstrap_metadata(state_db, catalog_db, pre["thread_id"], pre["host_id"])
    return {"journal": str(journal_path), "backups": backups, "status": journal["status"], "after": after}


def rollback_bootstrap(journal_path: Path) -> dict[str, Any]:
    journal = read_json(journal_path)
    if journal.get("stage") != "thread-bootstrap-metadata" or journal.get("status") != "committed":
        raise ValueError("journal is not a committed bootstrap stage")
    changes = journal["changes"]
    for name in ("catalog", "state"):
        operation = next((item for item in changes if item["database"] == name), None)
        if operation:
            live = operation.get("database_path")
            if not live:
                raise ValueError("journal lacks live database path for guarded rollback")
            _apply_one(Path(live), _reverse(operation))
    journal["status"] = "rolled-back"; write_json(journal_path, journal)
    return {"status": "rolled-back"}
