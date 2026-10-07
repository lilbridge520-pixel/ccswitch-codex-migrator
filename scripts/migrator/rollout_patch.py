from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .migration_plan import apply_operations_to_bytes, verify_plan
from .models import file_state, write_json
from .sqlite_projection import session_state


def apply_rollout(plan: dict[str, Any], confirm_sha: str, output_dir: Path) -> dict[str, Any]:
    verify_plan(plan)
    if plan["stage"] != "rollout":
        raise ValueError("plan is not a rollout plan")
    if confirm_sha.upper() != plan["plan_sha256"]:
        raise ValueError("confirmation SHA does not match plan")
    if not all([plan["codex_guard"]["known"], plan["rollout_schema_guard"]["known"], plan["sqlite_schema_guard"]["known"]]):
        raise ValueError("unknown version or schema is scan-only")
    rollout = Path(plan["source_pre"]["path"])
    current = file_state(rollout, plan["source_pre"]["last_ordinal"])
    if current != plan["source_pre"]:
        raise ValueError("rollout state changed; plan is invalid")
    sqlite_pre = plan["sqlite_pre"]
    now_db = session_state(Path(sqlite_pre["database"]), sqlite_pre["session_id"], rollout)
    if now_db != sqlite_pre:
        raise ValueError("SQLite session state changed; plan is invalid")
    candidate = apply_operations_to_bytes(rollout, plan["operations"], plan["item_findings"], plan.get("setting_findings", []))
    if hashlib.sha256(candidate).hexdigest().upper() != plan["source_post"]["sha256"] or len(candidate) != plan["source_post"]["bytes"]:
        raise ValueError("candidate does not match planned post-state")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = output_dir / "backups" / f"{rollout.name}.{timestamp}.bak"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rollout, backup)
    if hashlib.sha256(backup.read_bytes()).hexdigest().upper() != plan["source_pre"]["sha256"]:
        raise RuntimeError("rollout backup hash mismatch")
    temporary = rollout.with_name(rollout.name + ".ccswitch-migrator.tmp")
    temporary.write_bytes(candidate)
    os.replace(temporary, rollout)
    journal = {"format_version": 1, "stage": "rollout", "target": str(rollout), "backup": str(backup), "pre_sha256": plan["source_pre"]["sha256"], "post_sha256": plan["source_post"]["sha256"], "plan_sha256": plan["plan_sha256"]}
    journal_path = output_dir / "change-journal.json"
    write_json(journal_path, journal)
    return {"backup": str(backup), "journal": str(journal_path), "post_sha256": journal["post_sha256"]}
