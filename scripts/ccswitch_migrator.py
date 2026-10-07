from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from migrator.discovery import discover
from migrator.bootstrap_metadata import apply_bootstrap_plan, create_bootstrap_plan, rollback_bootstrap, scan_bootstrap_metadata
from migrator.migration_plan import create_plan, verify_plan
from migrator.models import read_json, write_json
from migrator.rollback import rollback_rollout
from migrator.rollout_patch import apply_rollout
from migrator.rollout_scan import scan_rollout
from migrator.sqlite_projection import online_backup, repair_offsets, rollback_offsets, session_state
from migrator.verification import record_verification, verify_gate
from migrator.version_guard import codex_guard


def detect_version(override: str | None) -> str:
    if override:
        return override
    result = subprocess.run(["codex", "--version"], capture_output=True, text=True, check=False)
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[-1] if output else "unknown"


def command_scan(args: argparse.Namespace) -> dict:
    version = codex_guard(detect_version(args.codex_version))
    if args.rollout:
        candidates = [{"rollout": str(Path(args.rollout).resolve()), "thread_id": args.thread_id, "session_id": args.session_id, "cwd": args.cwd, "match_kind": "explicit-rollout", "selection_status": "confirmed"}]
    else:
        candidates = discover(Path(args.codex_home), args.thread_id, args.cwd)
        if args.session_id:
            candidates = [candidate for candidate in candidates if candidate.get("session_id") == args.session_id]
    if len(candidates) != 1:
        report = {"codex_guard": version, "write_allowed": False, "candidates": candidates, "reason": "expected exactly one rollout candidate"}
        write_json(Path(args.output), report)
        return report
    candidate = candidates[0]
    if not candidate.get("session_id"):
        raise ValueError("session ID is required or must exist in session metadata")
    rollout = Path(candidate["rollout"])
    sqlite_path = Path(args.sqlite)
    provisional = session_state(sqlite_path, candidate["session_id"], rollout)
    first = provisional.get("first_ordinal") if provisional.get("session_found") else None
    rollout_report = scan_rollout(rollout, first)
    sqlite_state = session_state(sqlite_path, candidate["session_id"], rollout)
    write_allowed = all([
        candidate.get("selection_status", "confirmed") == "confirmed",
        version["known"], rollout_report["schema_guard"]["known"], sqlite_state["schema_guard"]["known"],
        sqlite_state.get("quick_check") == "ok", sqlite_state.get("session_found"),
    ])
    report = {"format_version": 1, "codex_guard": version, "candidate": candidate, "rollout_scan": rollout_report, "sqlite_state": sqlite_state, "projection_healthy": bool(sqlite_state.get("projection", {}).get("at_eof") and sqlite_state.get("projection", {}).get("ordinal_after_last") and not sqlite_state.get("offset_mismatches")), "write_allowed": write_allowed}
    if args.state_db or args.catalog_db:
        if not args.state_db or not args.catalog_db or not args.thread_id:
            raise ValueError("--state-db, --catalog-db, and --thread-id are required together")
        report["bootstrap_state"] = scan_bootstrap_metadata(Path(args.state_db), Path(args.catalog_db), args.thread_id, args.host_id)
        report["write_allowed"] = report["write_allowed"] and report["bootstrap_state"]["write_allowed"]
    if candidate.get("selection_status") == "candidate-only":
        report["scan_only_reason"] = "parent cwd matches are candidate-only; confirm by Thread ID and session metadata"
    write_json(Path(args.output), report)
    return report


def command_plan(args: argparse.Namespace) -> dict:
    plan = create_plan(Path(args.scan), Path(args.operations))
    write_json(Path(args.output), plan)
    return plan


def command_plan_bootstrap(args: argparse.Namespace) -> dict:
    plan = create_bootstrap_plan(Path(args.scan), Path(args.target))
    write_json(Path(args.output), plan)
    return plan


def command_apply_bootstrap(args: argparse.Namespace) -> dict:
    return apply_bootstrap_plan(read_json(Path(args.plan)), args.confirm_plan_sha, Path(args.output_dir))


def command_apply(args: argparse.Namespace) -> dict:
    return apply_rollout(read_json(Path(args.plan)), args.confirm_plan_sha, Path(args.output_dir))


def command_record_verification(args: argparse.Namespace) -> dict:
    gate = record_verification(read_json(Path(args.plan)), read_json(Path(args.evidence)))
    write_json(Path(args.output), gate)
    return gate


def command_repair_projection(args: argparse.Namespace) -> dict:
    plan = read_json(Path(args.plan))
    verify_plan(plan)
    if args.confirm_plan_sha.upper() != plan["plan_sha256"]:
        raise ValueError("confirmation SHA does not match plan")
    gate = read_json(Path(args.fresh_gate))
    verify_gate(gate, plan)
    if plan["stage"] != "projection":
        raise ValueError("plan is not a projection plan")
    state = plan["sqlite_pre"]
    db = Path(state["database"])
    rollout = Path(plan["source_pre"]["path"])
    current = session_state(db, state["session_id"], rollout)
    if current != state:
        raise ValueError("SQLite session state changed; plan is invalid")
    backup = Path(args.output_dir) / "backups" / "thread_history.sqlite"
    online_backup(db, backup)
    if session_state(backup, state["session_id"], rollout)["quick_check"] != "ok":
        raise RuntimeError("SQLite online backup failed validation")
    changed = repair_offsets(db, state["session_id"], plan["operations"])
    after = session_state(db, state["session_id"], rollout)
    if after["offset_mismatches"]:
        raise RuntimeError("projection offsets remain inconsistent")
    if after["items_hash"] != state["items_hash"] or after["projection"] != state["projection"] or after["realtime_hash"] != state["realtime_hash"]:
        raise RuntimeError("non-target projection data changed")
    import hashlib
    backup_sha = hashlib.sha256(backup.read_bytes()).hexdigest().upper()
    journal = {"format_version": 1, "stage": "projection", "database": str(db), "session_id": state["session_id"], "repairs": plan["operations"], "backup": str(backup), "backup_sha256": backup_sha, "plan_sha256": plan["plan_sha256"]}
    journal_path = Path(args.output_dir) / "projection-change-journal.json"
    write_json(journal_path, journal)
    return {"backup": str(backup), "backup_sha256": backup_sha, "journal": str(journal_path), "rows_updated": changed, "quick_check": after["quick_check"]}


def command_rollback_projection(args: argparse.Namespace) -> dict:
    journal = read_json(Path(args.journal))
    if journal.get("stage") != "projection":
        raise ValueError("journal is not a projection journal")
    changed = rollback_offsets(Path(journal["database"]), journal["session_id"], journal["repairs"])
    return {"rows_restored": changed, "database": journal["database"], "session_id": journal["session_id"]}


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ccswitch-codex-migrator")
    sub = root.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("--codex-home", default=str(Path.home() / ".codex")); scan.add_argument("--thread-id"); scan.add_argument("--cwd")
    scan.add_argument("--rollout"); scan.add_argument("--session-id"); scan.add_argument("--sqlite", required=True); scan.add_argument("--state-db"); scan.add_argument("--catalog-db"); scan.add_argument("--host-id", default="local"); scan.add_argument("--codex-version"); scan.add_argument("--output", required=True)
    plan = sub.add_parser("plan"); plan.add_argument("--scan", required=True); plan.add_argument("--operations", required=True); plan.add_argument("--output", required=True)
    plan_bootstrap = sub.add_parser("plan-bootstrap"); plan_bootstrap.add_argument("--scan", required=True); plan_bootstrap.add_argument("--target", required=True); plan_bootstrap.add_argument("--output", required=True)
    apply_bootstrap = sub.add_parser("apply-bootstrap"); apply_bootstrap.add_argument("--plan", required=True); apply_bootstrap.add_argument("--confirm-plan-sha", required=True); apply_bootstrap.add_argument("--output-dir", required=True)
    apply = sub.add_parser("apply-rollout"); apply.add_argument("--plan", required=True); apply.add_argument("--confirm-plan-sha", required=True); apply.add_argument("--output-dir", required=True)
    verify = sub.add_parser("record-verification"); verify.add_argument("--plan", required=True); verify.add_argument("--evidence", required=True); verify.add_argument("--output", required=True)
    repair = sub.add_parser("repair-projection"); repair.add_argument("--plan", required=True); repair.add_argument("--fresh-gate", required=True); repair.add_argument("--confirm-plan-sha", required=True); repair.add_argument("--output-dir", required=True)
    rollback = sub.add_parser("rollback-rollout"); rollback.add_argument("--journal", required=True)
    rollback_projection = sub.add_parser("rollback-projection"); rollback_projection.add_argument("--journal", required=True)
    rollback_bootstrap_parser = sub.add_parser("rollback-bootstrap"); rollback_bootstrap_parser.add_argument("--journal", required=True)
    return root


def main() -> int:
    args = build_parser().parse_args()
    handlers = {
        "scan": command_scan,
        "plan": command_plan,
        "plan-bootstrap": command_plan_bootstrap,
        "apply-bootstrap": command_apply_bootstrap,
        "apply-rollout": command_apply,
        "record-verification": command_record_verification,
        "repair-projection": command_repair_projection,
        "rollback-rollout": lambda value: rollback_rollout(read_json(Path(value.journal))),
        "rollback-projection": command_rollback_projection,
        "rollback-bootstrap": lambda value: rollback_bootstrap(Path(value.journal)),
    }
    try:
        result = handlers[args.command](args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({"status": "stopped", "error": str(error)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
