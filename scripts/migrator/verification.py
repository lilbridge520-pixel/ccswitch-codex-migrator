from __future__ import annotations

from pathlib import Path
from typing import Any

from .migration_plan import verify_plan
from .models import canonical_sha256, file_state


def record_verification(plan: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    verify_plan(plan)
    required = ["old_process_uuid", "new_process_uuid", "thread_resume_success", "reconstruction_success"]
    missing = [key for key in required if key not in evidence]
    if missing:
        raise ValueError(f"missing verification evidence: {missing}")
    if evidence["old_process_uuid"] == evidence["new_process_uuid"]:
        raise ValueError("fresh-process gate failed: process identity did not change")
    if not evidence["thread_resume_success"] or not evidence["reconstruction_success"]:
        raise ValueError("fresh resume/reconstruction gate failed")
    phase = evidence.get("phase", "fresh")
    if phase not in {"fresh", "final"}:
        raise ValueError("verification phase must be fresh or final")
    if phase == "final":
        final_required = ["remote_compact_success", "assistant_reply_success", "compatibility_warnings", "projection_warnings"]
        missing_final = [key for key in final_required if key not in evidence]
        if missing_final:
            raise ValueError(f"missing final verification evidence: {missing_final}")
        if not evidence["remote_compact_success"] or not evidence["assistant_reply_success"]:
            raise ValueError("final compact/reply gate failed")
        if evidence["compatibility_warnings"] or evidence["projection_warnings"]:
            raise ValueError("final verification contains compatibility or projection warnings")
    expected = plan["source_post"] if plan["stage"] == "rollout" else plan["source_pre"]
    source_path = Path(expected["path"])
    if phase == "fresh":
        current = file_state(source_path, expected["last_ordinal"])
        if current != expected:
            raise ValueError("source state changed before fresh verification")
    else:
        import hashlib
        data = source_path.read_bytes()
        if len(data) < expected["bytes"] or hashlib.sha256(data[: expected["bytes"]]).hexdigest().upper() != expected["sha256"]:
            raise ValueError("source was rewritten instead of append-only after fresh verification")
        current = file_state(source_path, None)
    gate = {"format_version": 1, "phase": phase, "plan_sha256": plan["plan_sha256"], "source_state": current, "evidence": evidence}
    gate["gate_sha256"] = canonical_sha256(gate)
    return gate


def verify_gate(gate: dict[str, Any], plan: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in gate.items() if key != "gate_sha256"}
    if canonical_sha256(unsigned) != gate.get("gate_sha256"):
        raise ValueError("verification gate SHA is invalid")
    if gate.get("plan_sha256") != plan.get("plan_sha256"):
        raise ValueError("verification gate belongs to another plan")
