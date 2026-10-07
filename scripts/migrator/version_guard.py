from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any


SUPPORTED_CODEX = re.compile(r"^codex-cli 0\.160\.\d+(?:[-+].*)?$")
SUPPORTED_CURRENT_SOURCE = re.compile(r"^0\.160\.\d+(?:[-+].*)?$")
KNOWN_EVENTS = {
    "session_meta", "thread_settings_applied", "turn_context", "response_item",
    "event_msg", "compacted", "token_usage_record", "world_state",
}
EXPECTED_TABLES = {
    "thread_history_projection_state": ["thread_id", "next_rollout_byte_offset", "next_rollout_ordinal"],
    "thread_items": ["thread_id", "turn_id", "item_id", "rollout_ordinal", "created_at_ms", "item_json", "item_type", "updated_at_ordinal", "started_at_ms", "completed_at_ms"],
    "thread_turns": ["thread_id", "turn_id", "rollout_ordinal", "status", "error_json", "started_at", "completed_at", "duration_ms", "first_user_item_id", "final_agent_item_id", "rollout_byte_offset", "rollout_end_ordinal", "rollout_end_byte_offset"],
    "thread_realtime_items": ["thread_id", "item_id", "rollout_ordinal", "created_at_ms", "item_type", "item_json"],
}

# Exact producer versions plus exact critical-schema fingerprints. Never widen
# these to 0.155.* or 0.158.* ranges.
COMMON_LEGACY_FINGERPRINTS = {
    "session_meta": {"4C5111E6E1E4CFB226C88237D6BCAE289154999AEA3A985FE74B85E76232B3A4"},
    "turn_context": {"402088BA4DEDC8CD5393FC4F64CC10DE3762B1F3727AC5CC896341C91BD26389"},
    "event_msg/thread_settings_applied": {"CDD6860EE8B3488775B2C16B387F348924561EB8C21B9C37D3D6F7C93DC7EE74"},
    "response_item/message": {"060458FA2AD4BD60CBF0AC6C091616F8F80DCE7326B572C753269F73586DCF99"},
    "response_item/reasoning": {"B76012E25CC066143A0F4990963DD619C3D91D6EFB0102B9668712B7E2300DB0"},
    "response_item/function_call": {"55B2653A23B26D0021A980D8B05F7C3618E6115CF385BB5F28FA3A99E3055E6C"},
}
# These records are appended by the audited official 0.160 runtime after a
# successful remote compact. They are never a producer-version wildcard: a
# legacy rollout may use them only at or after an exact runtime compact
# boundary in the same file.
OFFICIAL_RUNTIME_APPEND_FINGERPRINTS = {
    "compacted": {"B56BF279657A1BCCE3D66E9B9A555829E026B68E9696C7DAEE4B5D13F00AE2E4"},
    "turn_context": {"56FE98C04EBB5628F38D4234D5D21F016A84061B6C8ADE3125A12FC6DE700CB3"},
    "response_item/message": {"F9A91FF715CCFB9762534ECD9E600B103826F5132D2B604AD427BBEFC3087040"},
}
LEGACY_SOURCE_PROFILES: dict[str, dict[str, set[str]]] = {
    "0.155.0-alpha.9.2": {
        **COMMON_LEGACY_FINGERPRINTS,
        "session_meta": COMMON_LEGACY_FINGERPRINTS["session_meta"] | {
            "AC621621A02D95881B607D95FFC5B2F061B133754749FBA155559043AF8D7ECB",
            "FD11317ED8EDDA8FCCA612A89C13CECCDB00AEFE8406071D397D5368F5528A93",
        },
        "compacted": {
            "C28D5D5E7789072B5848A3AB93E8CF292216F8D5126C014C7D102DDC3225E47D",
            "DE9CF4BACBA615F56026451F99DE3E2113FBDBBBF0F50AB12DF32F85D388AA54",
        },
    },
    "0.155.0-alpha.16.4": dict(COMMON_LEGACY_FINGERPRINTS),
    "0.158.0-alpha.2.1": dict(COMMON_LEGACY_FINGERPRINTS),
}


def codex_guard(version: str) -> dict[str, Any]:
    normalized = version.strip()
    return {"known": bool(SUPPORTED_CODEX.fullmatch(normalized)), "version": normalized, "supported_pattern": "codex-cli 0.160.x"}


def _scalar_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _object_types(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, child in sorted(value.items()):
        if isinstance(child, list):
            element_shapes: list[Any] = []
            for item in child:
                shape = {name: _scalar_type(part) for name, part in sorted(item.items())} if isinstance(item, dict) else _scalar_type(item)
                if shape not in element_shapes:
                    element_shapes.append(shape)
            result[key] = {"type": "array", "elements": sorted(element_shapes, key=lambda item: json.dumps(item, sort_keys=True))}
        else:
            result[key] = _scalar_type(child)
    return result


def critical_schema_descriptor(record: dict[str, Any]) -> dict[str, Any] | None:
    event = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    key: str | None = None
    if event in {"session_meta", "turn_context", "thread_settings_applied", "compacted"}:
        key = str(event)
    elif event == "event_msg" and payload.get("type") in {"thread_settings_applied", "context_compacted", "compacted"}:
        key = f"event_msg/{payload.get('type')}"
    elif event == "response_item" and payload.get("type") in {"message", "reasoning", "function_call"} and "id" in payload:
        key = f"response_item/{payload.get('type')}"
    if key is None:
        return None
    return {"event": key, "payload": _object_types(payload)}


def critical_schema_fingerprint(record: dict[str, Any]) -> tuple[str, str] | None:
    descriptor = critical_schema_descriptor(record)
    if descriptor is None:
        return None
    raw = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    return descriptor["event"], hashlib.sha256(raw).hexdigest().upper()


def rollout_guard(records: list[dict[str, Any]]) -> dict[str, Any]:
    unknown: set[str] = set()
    invalid: list[int] = []
    unverified_items: list[dict[str, Any]] = []
    critical: list[dict[str, Any]] = []
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            invalid.append(index)
            continue
        event_type = record["type"]
        if event_type not in KNOWN_EVENTS:
            unknown.add(event_type)
        if event_type in {"thread_settings_applied", "turn_context", "response_item", "event_msg", "compacted"} and not isinstance(record.get("payload"), dict):
            invalid.append(index)
            continue
        payload = record.get("payload") or {}
        if event_type == "response_item" and isinstance(payload.get("id"), str):
            item_id = payload["id"]
            if (payload.get("type") == "custom_tool_call" and item_id.startswith("ctc_call_")) or (payload.get("type") == "custom_tool_call_output" and item_id.startswith("ctco_")):
                unverified_items.append({"line": index, "payload_type": payload.get("type"), "id_family": "ctc_call_*" if item_id.startswith("ctc_call_") else "ctco_*"})
        fingerprint = critical_schema_fingerprint(record)
        if fingerprint:
            critical.append({"line": index, "event": fingerprint[0], "fingerprint": fingerprint[1]})

    source_versions = sorted({str((record.get("payload") or {}).get("cli_version")) for record in records if record.get("type") == "session_meta" and (record.get("payload") or {}).get("cli_version")})
    exact_profile_version = source_versions[0] if len(source_versions) == 1 and source_versions[0] in LEGACY_SOURCE_PROFILES else None
    current_source_known = bool(source_versions) and all(SUPPORTED_CURRENT_SOURCE.fullmatch(version) for version in source_versions)
    profile_mismatches: list[dict[str, Any]] = []
    runtime_append_boundaries: list[int] = []
    if exact_profile_version:
        profile = LEGACY_SOURCE_PROFILES[exact_profile_version]
        runtime_boundary = None
        for item in critical:
            if item["event"] == "compacted" and item["fingerprint"] in OFFICIAL_RUNTIME_APPEND_FINGERPRINTS["compacted"]:
                runtime_boundary = item["line"]
                runtime_append_boundaries.append(item["line"])
            allowed = profile.get(item["event"])
            legacy_known = bool(allowed and item["fingerprint"] in allowed)
            runtime_known = bool(
                runtime_boundary is not None
                and item["line"] >= runtime_boundary
                and item["fingerprint"] in OFFICIAL_RUNTIME_APPEND_FINGERPRINTS.get(item["event"], set())
            )
            if not legacy_known and not runtime_known:
                profile_mismatches.append(item)
    source_version_known = current_source_known or exact_profile_version is not None
    critical_schema_known = current_source_known or bool(exact_profile_version and not profile_mismatches)
    event_names = sorted({record.get("type") for record in records if isinstance(record, dict)})
    event_fingerprint = hashlib.sha256(json.dumps(event_names).encode()).hexdigest().upper()
    critical_set = sorted({f"{item['event']}:{item['fingerprint']}" for item in critical})
    critical_set_fingerprint = hashlib.sha256(json.dumps(critical_set).encode()).hexdigest().upper()
    known = not unknown and not invalid and source_version_known and critical_schema_known and not unverified_items
    return {
        "known": known,
        "unknown_event_types": sorted(unknown),
        "invalid_lines": invalid,
        "source_versions": source_versions,
        "source_version_known": source_version_known,
        "source_profile": exact_profile_version or ("current-0.160.x" if current_source_known else None),
        "critical_schema_known": critical_schema_known,
        "critical_schema_mismatches": profile_mismatches,
        "runtime_append_boundaries": runtime_append_boundaries,
        "critical_schema_records": critical,
        "critical_schema_set_fingerprint": critical_set_fingerprint,
        "unverified_response_items": unverified_items,
        "fingerprint": event_fingerprint,
    }


def sqlite_guard(connection: sqlite3.Connection) -> dict[str, Any]:
    observed: dict[str, list[str]] = {}
    mismatches: dict[str, Any] = {}
    for table, expected in EXPECTED_TABLES.items():
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        observed[table] = columns
        if columns != expected:
            mismatches[table] = {"expected": expected, "observed": columns}
    fingerprint = hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest().upper()
    return {"known": not mismatches, "mismatches": mismatches, "fingerprint": fingerprint}
