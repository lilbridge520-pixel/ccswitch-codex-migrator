from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

from .models import file_state
from .version_guard import rollout_guard


def parse_rollout(path: Path) -> tuple[bytes, list[bytes], list[dict[str, Any]], list[int], list[int]]:
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    starts: list[int] = []
    ends: list[int] = []
    offset = 0
    for number, raw in enumerate(lines, 1):
        starts.append(offset)
        try:
            record = json.loads(raw)
        except Exception as error:
            raise ValueError(f"malformed JSONL line {number}: {error}") from error
        if not isinstance(record, dict):
            raise ValueError(f"JSONL line {number} is not an object")
        records.append(record)
        offset += len(raw)
        ends.append(offset)
    return data, lines, records, starts, ends


def walk(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + (index,))
    else:
        yield path, value


def checkpoint_index(records: list[dict[str, Any]]) -> int:
    checkpoints = []
    for index, record in enumerate(records):
        payload = record.get("payload") or {}
        if record.get("type") == "compacted" or (record.get("type") == "event_msg" and payload.get("type") in {"context_compacted", "compacted"}):
            checkpoints.append(index)
    return checkpoints[-1] if checkpoints else -1


def scan_rollout(path: Path, first_ordinal: int | None = None) -> dict[str, Any]:
    data, lines, records, starts, ends = parse_rollout(path)
    checkpoint = checkpoint_index(records)
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    occurrences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    settings: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        line = index + 1
        ordinal = first_ordinal + index if first_ordinal is not None else None
        event_type = record.get("type")
        payload = record.get("payload") or {}
        if event_type == "session_meta":
            for key in ("model_provider",):
                if key in payload:
                    settings.append({"line": line, "ordinal": ordinal, "event_type": event_type, "path": ["payload", key], "value": payload[key], "in_compact_window": index > checkpoint})
        if event_type in {"turn_context", "thread_settings_applied"}:
            for key in ("model", "model_provider", "effort", "reasoning_effort"):
                if key in payload:
                    settings.append({"line": line, "ordinal": ordinal, "event_type": event_type, "path": ["payload", key], "value": payload[key], "in_compact_window": index > checkpoint})
            if event_type == "turn_context":
                collaboration = payload.get("collaboration_mode")
                collaboration_settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
                if isinstance(collaboration_settings, dict):
                    for key in ("model", "reasoning_effort", "model_provider", "model_provider_id"):
                        if key in collaboration_settings:
                            settings.append({"line": line, "ordinal": ordinal, "event_type": event_type, "path": ["payload", "collaboration_mode", "settings", key], "value": collaboration_settings[key], "in_compact_window": index > checkpoint})
        if event_type == "event_msg" and payload.get("type") == "thread_settings_applied" and isinstance(payload.get("thread_settings"), dict):
            thread_settings = payload["thread_settings"]
            for key in ("model", "model_provider_id", "reasoning_effort"):
                if key in thread_settings:
                    settings.append({"line": line, "ordinal": ordinal, "event_type": "event_msg/thread_settings_applied", "path": ["payload", "thread_settings", key], "value": thread_settings[key], "in_compact_window": index > checkpoint})
            collaboration = thread_settings.get("collaboration_mode")
            collaboration_settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
            if isinstance(collaboration_settings, dict):
                for key in ("model", "reasoning_effort"):
                    if key in collaboration_settings:
                        settings.append({"line": line, "ordinal": ordinal, "event_type": "event_msg/thread_settings_applied", "path": ["payload", "thread_settings", "collaboration_mode", "settings", key], "value": collaboration_settings[key], "in_compact_window": index > checkpoint})
        if event_type == "world_state":
            for field_path, value in walk(payload):
                if field_path and field_path[-1] in {"model", "model_provider", "model_provider_id", "reasoning_effort"}:
                    settings.append({"line": line, "ordinal": ordinal, "event_type": event_type, "path": ["payload", *field_path], "value": value, "in_compact_window": index > checkpoint, "report_only": True})
        for field_path, value in walk(record):
            if isinstance(value, str):
                occurrences[value].append({"line": line, "path": list(field_path), "event_type": event_type})
        if event_type == "response_item" and isinstance(payload, dict) and payload.get("type") in {"message", "reasoning", "function_call", "custom_tool_call", "custom_tool_call_output"} and isinstance(payload.get("id"), str):
            item_id = payload["id"]
            candidates[item_id].append({"line": line, "ordinal": ordinal, "event_type": event_type, "payload_type": payload.get("type"), "path": ["payload", "id"], "in_compact_window": index > checkpoint})
    findings = []
    unverified_findings = []
    for item_id, locations in candidates.items():
        self_locations = {(location["line"], tuple(location["path"])) for location in locations}
        refs = [entry for entry in occurrences[item_id] if (entry["line"], tuple(entry["path"])) not in self_locations]
        mirror_refs = [entry for entry in refs if entry["event_type"] == "event_msg" and entry["path"] == ["payload", "item", "id"]]
        semantic_refs = [entry for entry in refs if entry not in mirror_refs]
        known_pattern = "chat-completions-derived" if "chatcmpl" in item_id else "legacy-function-call" if item_id.startswith("fc_call_") else "other"
        finding = {"id": item_id, "format": known_pattern, "locations": locations, "references": refs, "mirror_references": mirror_refs, "semantic_references": semantic_refs, "unreferenced": not refs, "safe_to_remove_transport_id": not semantic_refs}
        if any(location["payload_type"] in {"custom_tool_call", "custom_tool_call_output"} for location in locations):
            finding["classification"] = "discovered-unverified"
            finding["deletion_allowed"] = False
            unverified_findings.append(finding)
        else:
            findings.append(finding)
    latest_by_path: dict[tuple[str, ...], int] = {}
    for position, setting in enumerate(settings):
        if setting["event_type"] == "turn_context" and setting["path"][:3] == ["payload", "collaboration_mode", "settings"]:
            latest_by_path[tuple(setting["path"])] = position
    for position, setting in enumerate(settings):
        setting["is_effective"] = latest_by_path.get(tuple(setting["path"])) == position if setting["event_type"] == "turn_context" and setting["path"][:3] == ["payload", "collaboration_mode", "settings"] else None
    last_ordinal = first_ordinal + len(records) - 1 if first_ordinal is not None and records else None
    return {
        "rollout": file_state(path, last_ordinal),
        "line_count": len(records),
        "first_ordinal": first_ordinal,
        "latest_checkpoint_line": checkpoint + 1 if checkpoint >= 0 else None,
        "schema_guard": rollout_guard(records),
        "settings": settings,
        "item_findings": findings,
        "unverified_item_findings": unverified_findings,
        "line_offsets": {"starts": starts, "ends": ends},
        "raw_bytes": len(data),
    }
