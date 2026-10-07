from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .models import canonical_sha256, read_json
from .rollout_scan import parse_rollout


ALLOWED_ITEM_TYPES = {"message", "reasoning", "function_call"}
ALLOWED_LEGACY_ID_FORMATS = {"chat-completions-derived", "legacy-function-call"}


def get_path(record: dict[str, Any], path: list[str]) -> Any:
    current: Any = record
    for key in path:
        current = current[key]
    return current


def mutate(record: dict[str, Any], operation: dict[str, Any]) -> None:
    current: Any = record
    path = operation["path"]
    for key in path[:-1]:
        current = current[key]
    if operation["kind"] == "delete":
        del current[path[-1]]
    else:
        current[path[-1]] = operation["value"]


def validate_operation(record: dict[str, Any], operation: dict[str, Any], findings: list[dict[str, Any]], setting_findings: list[dict[str, Any]] | None = None) -> None:
    path = operation["path"]
    if operation["kind"] == "set":
        allowed = False
        if record.get("type") == "session_meta" and path == ["payload", "model_provider"]:
            allowed = True
        elif record.get("type") == "turn_context" and path in (["payload", "model"], ["payload", "model_provider"], ["payload", "effort"]):
            allowed = True
        elif record.get("type") == "turn_context" and path in (
            ["payload", "collaboration_mode", "settings", "model"],
            ["payload", "collaboration_mode", "settings", "reasoning_effort"],
            ["payload", "collaboration_mode", "settings", "model_provider"],
            ["payload", "collaboration_mode", "settings", "model_provider_id"],
        ):
            matching = [item for item in (setting_findings or []) if item.get("line") == operation.get("line") and item.get("path") == path]
            allowed = len(matching) == 1 and matching[0].get("is_effective") is True and not matching[0].get("report_only")
        elif record.get("type") == "thread_settings_applied" and path in (["payload", "model"], ["payload", "model_provider"], ["payload", "reasoning_effort"]):
            allowed = True
        elif record.get("type") == "event_msg" and (record.get("payload") or {}).get("type") == "thread_settings_applied" and path in (
            ["payload", "thread_settings", "model"],
            ["payload", "thread_settings", "model_provider_id"],
            ["payload", "thread_settings", "reasoning_effort"],
            ["payload", "thread_settings", "collaboration_mode", "settings", "model"],
            ["payload", "thread_settings", "collaboration_mode", "settings", "reasoning_effort"],
        ):
            allowed = True
        if not allowed:
            raise ValueError("unsupported set operation")
    elif operation["kind"] == "delete":
        payload = record.get("payload") or {}
        if record.get("type") != "response_item" or payload.get("type") not in ALLOWED_ITEM_TYPES or path != ["payload", "id"]:
            raise ValueError("unsupported delete operation")
        matching = [item for item in findings if item["id"] == operation["expected"]]
        if not matching or matching[0].get("format") not in ALLOWED_LEGACY_ID_FORMATS or not matching[0].get("safe_to_remove_transport_id", matching[0].get("unreferenced")):
            raise ValueError("item id is missing, not a verified legacy transport id, or has semantic references")
    else:
        raise ValueError("unsupported rollout operation kind")
    if get_path(record, path) != operation["expected"]:
        raise ValueError("operation expected value does not match scan")


def apply_operations_to_bytes(rollout: Path, operations: list[dict[str, Any]], findings: list[dict[str, Any]], setting_findings: list[dict[str, Any]] | None = None) -> bytes:
    _, raw_lines, records, _, _ = parse_rollout(rollout)
    grouped: dict[int, list[dict[str, Any]]] = {}
    for operation in operations:
        grouped.setdefault(int(operation["line"]), []).append(operation)
    output = list(raw_lines)
    for line, line_operations in grouped.items():
        if line < 1 or line > len(records):
            raise ValueError(f"operation line out of range: {line}")
        record = copy.deepcopy(records[line - 1])
        for operation in line_operations:
            validate_operation(record, operation, findings, setting_findings)
            mutate(record, operation)
        newline = b"\r\n" if raw_lines[line - 1].endswith(b"\r\n") else b"\n" if raw_lines[line - 1].endswith(b"\n") else b""
        output[line - 1] = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + newline
    candidate = b"".join(output)
    for index, raw in enumerate(candidate.splitlines(), 1):
        try:
            json.loads(raw)
        except Exception as error:
            raise ValueError(f"candidate JSON error at line {index}: {error}") from error
    return candidate


def create_plan(scan_path: Path, operations_path: Path) -> dict[str, Any]:
    scan = read_json(scan_path)
    operation_document = read_json(operations_path)
    migration_target = None
    if isinstance(operation_document, dict):
        migration_target = operation_document.get("target")
        operations = operation_document.get("operations")
        if not isinstance(migration_target, dict) or set(migration_target) != {"provider", "model", "reasoning"} or not all(isinstance(value, str) and value for value in migration_target.values()):
            raise ValueError("target must contain non-empty provider, model, and reasoning strings")
    else:
        operations = operation_document
    if not isinstance(operations, list) or not operations:
        raise ValueError("operations must be a non-empty JSON array")
    if not scan["write_allowed"]:
        raise ValueError("scan guards are not write-safe")
    if scan.get("candidate", {}).get("selection_status", "confirmed") != "confirmed":
        raise ValueError("candidate-only cwd matches cannot enter a migration plan")
    rollout = Path(scan["rollout_scan"]["rollout"]["path"])
    projection_stage = all(operation.get("kind") == "update_offset" for operation in operations)
    if projection_stage:
        expected = scan["sqlite_state"]["offset_mismatches"]
        if operations != expected:
            raise ValueError("projection operations must exactly match the current scan")
        candidate = rollout.read_bytes()
        stage = "projection"
    else:
        if any(operation.get("kind") == "update_offset" for operation in operations):
            raise ValueError("do not mix rollout and projection operations")
        candidate = apply_operations_to_bytes(rollout, operations, scan["rollout_scan"]["item_findings"], scan["rollout_scan"].get("settings", []))
        stage = "rollout"
    plan = {
        "format_version": 1,
        "stage": stage,
        "codex_guard": scan["codex_guard"],
        "rollout_schema_guard": scan["rollout_scan"]["schema_guard"],
        "sqlite_schema_guard": scan["sqlite_state"]["schema_guard"],
        "source_pre": scan["rollout_scan"]["rollout"],
        "source_post": {**scan["rollout_scan"]["rollout"], "sha256": hashlib.sha256(candidate).hexdigest().upper(), "bytes": len(candidate)},
        "sqlite_pre": scan["sqlite_state"],
        "item_findings": scan["rollout_scan"]["item_findings"],
        "setting_findings": scan["rollout_scan"].get("settings", []),
        "operations": operations,
    }
    if migration_target is not None:
        plan["migration_target"] = migration_target
    plan["plan_sha256"] = canonical_sha256(plan)
    return plan


def verify_plan(plan: dict[str, Any]) -> None:
    expected = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if not expected or canonical_sha256(unsigned) != expected:
        raise ValueError("migration plan SHA-256 is invalid")
