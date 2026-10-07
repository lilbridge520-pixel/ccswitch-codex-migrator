from __future__ import annotations

import json
import ntpath
from pathlib import Path
from typing import Any


def metadata(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    break
                record = json.loads(line)
                if record.get("type") == "session_meta":
                    payload = record.get("payload") or {}
                    return {
                        "thread_id": payload.get("thread_id") or payload.get("id"),
                        "session_id": payload.get("session_id") or payload.get("id"),
                        "cwd": payload.get("cwd"),
                        "title": payload.get("title"),
                    }
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return None


def _normalized_windows_path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value))


def _cwd_match(recorded: str, requested: str) -> str | None:
    recorded_norm = _normalized_windows_path(recorded)
    requested_norm = _normalized_windows_path(requested)
    if recorded_norm == requested_norm:
        return "exact-cwd"
    try:
        if ntpath.commonpath([recorded_norm, requested_norm]) == recorded_norm:
            return "parent-cwd-candidate"
    except ValueError:
        pass
    return None


def _titles(codex_home: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    index = codex_home / "session_index.jsonl"
    if not index.exists():
        return result
    try:
        with index.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if isinstance(row.get("id"), str):
                    result[row["id"]] = {"title": row.get("thread_name"), "index_updated_at": row.get("updated_at")}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return result


def discover(codex_home: Path, thread_id: str | None, cwd: str | None) -> list[dict[str, Any]]:
    if bool(thread_id) == bool(cwd):
        raise ValueError("provide exactly one of thread_id or cwd")
    result: list[dict[str, Any]] = []
    title_index = _titles(codex_home)
    root = codex_home / "sessions"
    for path in root.rglob("*.jsonl") if root.exists() else []:
        meta = metadata(path)
        if not meta:
            continue
        match_kind = "thread-id"
        selection_status = "confirmed"
        if thread_id:
            if meta.get("thread_id") != thread_id and thread_id not in path.name:
                continue
        else:
            recorded = str(meta.get("cwd", ""))
            match_kind = _cwd_match(recorded, str(cwd)) or ""
            if not match_kind:
                continue
            if match_kind == "parent-cwd-candidate":
                selection_status = "candidate-only"
        title = title_index.get(str(meta.get("thread_id")), {})
        result.append({**meta, **title, "rollout": str(path.resolve()), "match_kind": match_kind, "selection_status": selection_status})
    return sorted(result, key=lambda item: item["rollout"])
