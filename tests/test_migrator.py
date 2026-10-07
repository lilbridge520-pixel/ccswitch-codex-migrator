from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from migrator.migration_plan import apply_operations_to_bytes, create_plan
from migrator.bootstrap_metadata import CATALOG_TABLE_INFO, STATE_TABLE_INFO, _guarded_update, apply_bootstrap_plan, create_bootstrap_plan, rollback_bootstrap, scan_bootstrap_metadata
from migrator.discovery import discover
from migrator.models import write_json
from migrator.rollback import rollback_rollout
from migrator.rollout_patch import apply_rollout
from migrator.rollout_scan import parse_rollout, scan_rollout
from migrator.sqlite_projection import online_backup, repair_offsets, rollback_offsets, session_state
from migrator.verification import record_verification
from migrator.version_guard import codex_guard, critical_schema_fingerprint, rollout_guard, sqlite_guard


FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_legacy_rollout.jsonl"
OPERATIONS = ROOT / "tests" / "fixtures" / "synthetic_operations.json"
PRODUCER_FIXTURES = {
    version: ROOT / "tests" / "fixtures" / f"source-{version}.jsonl"
    for version in ("0.155.0-alpha.9.2", "0.155.0-alpha.16.4", "0.158.0-alpha.2.1")
}
RUNTIME_APPEND_FIXTURE = ROOT / "tests" / "fixtures" / "synthetic-runtime-append-0160.jsonl"
SESSION = "synthetic-session-0001"


def make_db(path: Path, rollout: Path, stale: bool = False) -> None:
    data, lines, _, starts, ends = parse_rollout(rollout)
    connection = sqlite3.connect(path)
    connection.executescript("""
    CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER NOT NULL,next_rollout_ordinal INTEGER NOT NULL);
    CREATE TABLE thread_items (thread_id TEXT NOT NULL,turn_id TEXT NOT NULL,item_id TEXT NOT NULL,rollout_ordinal INTEGER NOT NULL,created_at_ms INTEGER NOT NULL,item_json TEXT NOT NULL,item_type TEXT NOT NULL DEFAULT '',updated_at_ordinal INTEGER NOT NULL DEFAULT 0,started_at_ms INTEGER,completed_at_ms INTEGER,PRIMARY KEY(thread_id,turn_id,item_id));
    CREATE TABLE thread_turns (thread_id TEXT NOT NULL,turn_id TEXT NOT NULL,rollout_ordinal INTEGER NOT NULL,status TEXT NOT NULL,error_json TEXT,started_at INTEGER,completed_at INTEGER,duration_ms INTEGER,first_user_item_id TEXT,final_agent_item_id TEXT,rollout_byte_offset INTEGER,rollout_end_ordinal INTEGER,rollout_end_byte_offset INTEGER,PRIMARY KEY(thread_id,turn_id));
    CREATE TABLE thread_realtime_items (thread_id TEXT NOT NULL,item_id TEXT NOT NULL,rollout_ordinal INTEGER NOT NULL,created_at_ms INTEGER NOT NULL,item_type TEXT NOT NULL,item_json TEXT NOT NULL,PRIMARY KEY(thread_id,item_id));
    """)
    first = 100
    connection.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", (SESSION, len(data), first + len(lines)))
    delta = 3 if stale else 0
    connection.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (SESSION, "synthetic-turn", first + 3, "completed", None, 1, 2, 1, None, "msg_local_synthetic", starts[3] + delta, first + 8, ends[8] + delta))
    connection.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?,?,?)", (SESSION, "synthetic-turn", "msg_local_synthetic", first + 8, 1, json.dumps({"id":"msg_local_synthetic"}), "agentMessage", first + 8, 1, 2))
    connection.commit()
    connection.close()


def create_exact_table(connection: sqlite3.Connection, name: str, info: list[tuple]) -> None:
    primary = [column[0] for column in sorted((column for column in info if column[4]), key=lambda column: column[4])]
    definitions = []
    for column, kind, notnull, default, _ in info:
        part = f'"{column}" {kind}'
        if notnull: part += " NOT NULL"
        if default is not None: part += f" DEFAULT {default}"
        if len(primary) == 1 and primary[0] == column: part += " PRIMARY KEY"
        definitions.append(part)
    if len(primary) > 1: definitions.append("PRIMARY KEY (" + ",".join(f'\"{name}\"' for name in primary) + ")")
    connection.execute(f'CREATE TABLE "{name}" (' + ",".join(definitions) + ")")


def synthetic_value(column: tuple, thread_id: str) -> object:
    name, kind, notnull, default, _ = column
    fixed = {"id":thread_id,"thread_id":thread_id,"host_id":"local","model_provider":"OpenAI","model":"legacy-model","reasoning_effort":"medium","rollout_path":"C:/synthetic/rollout.jsonl","cli_version":"0.155.0-alpha.16.4","display_title":"Synthetic title","cwd":"C:/synthetic/study","source_kind":"vscode"}
    if name in fixed: return fixed[name]
    if default is not None:
        if default.startswith("'"): return default.strip("'")
        return int(default)
    if not notnull: return None
    if kind in {"INTEGER", "BOOLEAN"}: return 1
    if kind == "REAL": return 1.0
    return f"synthetic-{name}"


def make_bootstrap_dbs(root: Path, thread_id: str = "synthetic-thread-bootstrap") -> tuple[Path, Path]:
    state = root / "state_5.sqlite"; catalog = root / "codex-dev.db"
    for path, table, info in ((state,"threads",STATE_TABLE_INFO),(catalog,"local_thread_catalog",CATALOG_TABLE_INFO)):
        connection = sqlite3.connect(path); create_exact_table(connection, table, info)
        values = [synthetic_value(column, thread_id) for column in info]
        connection.execute(f'INSERT INTO {table} VALUES (' + ",".join("?" for _ in values) + ")", values)
        connection.commit(); connection.close()
    return state, catalog


class MigratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.rollout = self.root / "synthetic.jsonl"
        shutil.copy2(FIXTURE, self.rollout)
        self.db = self.root / "thread_history.sqlite"
        make_db(self.db, self.rollout)
        (self.root / "auth.json").write_text("AUTH-SENTINEL", encoding="utf-8")
        (self.root / "config.toml").write_text("CONFIG-SENTINEL", encoding="utf-8")
        self.state_db, self.catalog_db = make_bootstrap_dbs(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def scan_report(self) -> dict:
        sqlite_state = session_state(self.db, SESSION, self.rollout)
        rollout = scan_rollout(self.rollout, sqlite_state["first_ordinal"])
        return {"format_version":1,"codex_guard":codex_guard("codex-cli 0.160.0"),"candidate":{"thread_id":"synthetic-thread-0001","session_id":SESSION,"rollout":str(self.rollout)},"rollout_scan":rollout,"sqlite_state":sqlite_state,"write_allowed":True}

    def bootstrap_scan_report(self) -> dict:
        report = self.scan_report()
        report["candidate"]["thread_id"] = "synthetic-thread-bootstrap"
        report["bootstrap_state"] = scan_bootstrap_metadata(self.state_db, self.catalog_db, "synthetic-thread-bootstrap")
        return report

    def bootstrap_plan(self) -> dict:
        scan_path = self.root / "bootstrap-scan.json"; target_path = self.root / "target.json"
        write_json(scan_path, self.bootstrap_scan_report()); write_json(target_path, {"provider":"openai","model":"official-model","reasoning":"medium"})
        return create_bootstrap_plan(scan_path, target_path)

    def test_scan_detects_compact_window_and_legacy_examples(self) -> None:
        report = self.scan_report()["rollout_scan"]
        self.assertEqual(report["latest_checkpoint_line"], 3)
        self.assertEqual(len(report["item_findings"]), 3)
        self.assertTrue(all(item["unreferenced"] for item in report["item_findings"]))
        self.assertTrue(any(item["path"] == ["payload", "model_provider"] and item["value"] == "OpenAI" for item in report["settings"]))

    def test_unknown_version_and_event_are_scan_only(self) -> None:
        self.assertFalse(codex_guard("codex-cli 9.9.9")["known"])
        self.assertFalse(rollout_guard([{"type":"future_event","payload":{}}])["known"])
        records = [json.loads(line) for line in FIXTURE.read_bytes().splitlines()]
        records[0]["payload"]["cli_version"] = "9.9.9"
        self.assertFalse(rollout_guard(records)["known"])

    def test_exact_legacy_producer_profiles_require_matching_schema_fingerprints(self) -> None:
        for version, fixture in PRODUCER_FIXTURES.items():
            records = [json.loads(line) for line in fixture.read_bytes().splitlines()]
            guard = rollout_guard(records)
            self.assertTrue(guard["known"], (version, guard["critical_schema_mismatches"]))
            self.assertEqual(guard["source_profile"], version)
            self.assertTrue(guard["critical_schema_set_fingerprint"])
            self.assertTrue(all(critical_schema_fingerprint(record) for record in records))

        records = [json.loads(line) for line in PRODUCER_FIXTURES["0.155.0-alpha.16.4"].read_bytes().splitlines()]
        records[0]["payload"]["cli_version"] = "0.155.0-alpha.16.5"
        self.assertFalse(rollout_guard(records)["known"])
        records[0]["payload"]["cli_version"] = "0.155.0-alpha.16.4"
        records[3]["payload"]["content"] = "unexpected-shape"
        guard = rollout_guard(records)
        self.assertFalse(guard["known"])
        self.assertTrue(guard["critical_schema_mismatches"])

    def test_official_runtime_append_fingerprints_are_allowed_only_after_exact_compact_boundary(self) -> None:
        records = [json.loads(line) for line in RUNTIME_APPEND_FIXTURE.read_bytes().splitlines()]
        guard = rollout_guard(records)
        self.assertTrue(guard["known"])
        self.assertEqual(guard["runtime_append_boundaries"], [4])
        fingerprints = {item["fingerprint"] for item in guard["critical_schema_records"]}
        self.assertTrue({
            "B56BF279657A1BCCE3D66E9B9A555829E026B68E9696C7DAEE4B5D13F00AE2E4",
            "56FE98C04EBB5628F38D4234D5D21F016A84061B6C8ADE3125A12FC6DE700CB3",
            "F9A91FF715CCFB9762534ECD9E600B103826F5132D2B604AD427BBEFC3087040",
        }.issubset(fingerprints))
        moved_before_boundary = [records[0], records[1], records[2], records[4], records[3], records[5]]
        self.assertFalse(rollout_guard(moved_before_boundary)["known"])

    def test_unknown_runtime_append_fingerprint_remains_scan_only(self) -> None:
        records = [json.loads(line) for line in RUNTIME_APPEND_FIXTURE.read_bytes().splitlines()]
        records[-1]["payload"]["phase"] = 1
        guard = rollout_guard(records)
        self.assertFalse(guard["known"])
        self.assertTrue(any(item["line"] == 6 for item in guard["critical_schema_mismatches"]))

    def test_unknown_sqlite_schema_returns_scan_only_state(self) -> None:
        unknown = self.root / "unknown.sqlite"
        connection = sqlite3.connect(unknown)
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.commit(); connection.close()
        state = session_state(unknown, SESSION, self.rollout)
        self.assertFalse(state["schema_guard"]["known"])
        self.assertFalse(state["session_found"])
        self.assertEqual(state["scan_only_reason"], "unknown SQLite schema")

    def test_sqlite_schema_guard_rejects_unknown_columns(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE thread_history_projection_state(thread_id TEXT, next_rollout_byte_offset INTEGER, next_rollout_ordinal INTEGER, future_column TEXT)")
        self.assertFalse(sqlite_guard(connection)["known"])

    def test_plan_binds_source_and_sqlite_state(self) -> None:
        scan_path = self.root / "scan.json"
        write_json(scan_path, self.scan_report())
        plan = create_plan(scan_path, OPERATIONS)
        self.assertEqual(plan["source_pre"]["sha256"], hashlib.sha256(self.rollout.read_bytes()).hexdigest().upper())
        self.assertIn("items_hash", plan["sqlite_pre"])
        self.assertTrue(plan["plan_sha256"])

    def test_structured_patch_preserves_body_summary_call_id_and_event_id(self) -> None:
        report = self.scan_report()["rollout_scan"]
        operations = json.loads(OPERATIONS.read_text(encoding="utf-8"))
        candidate = apply_operations_to_bytes(self.rollout, operations, report["item_findings"])
        records = [json.loads(line) for line in candidate.splitlines()]
        self.assertEqual(records[4]["payload"]["content"][0]["text"], "Synthetic assistant body")
        self.assertEqual(records[5]["payload"]["summary"][0]["text"], "Synthetic summary")
        self.assertEqual(records[6]["payload"]["call_id"], "call_synthetic_keep")
        self.assertEqual(records[7]["payload"]["call_id"], "call_synthetic_keep")
        self.assertEqual(records[8]["payload"]["item"]["id"], "msg_local_synthetic")
        for index in (4, 5, 6):
            self.assertNotIn("id", records[index]["payload"])

    def test_apply_invalidates_when_rollout_changes(self) -> None:
        scan_path = self.root / "scan.json"
        write_json(scan_path, self.scan_report())
        plan = create_plan(scan_path, OPERATIONS)
        self.rollout.write_bytes(self.rollout.read_bytes() + b'{"type":"token_usage_record","payload":{}}\n')
        with self.assertRaisesRegex(ValueError, "state changed"):
            apply_rollout(plan, plan["plan_sha256"], self.root / "run")

    def test_apply_invalidates_when_sqlite_session_changes(self) -> None:
        scan_path = self.root / "scan.json"
        write_json(scan_path, self.scan_report())
        plan = create_plan(scan_path, OPERATIONS)
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE thread_items SET item_json=? WHERE thread_id=?", ('{"changed":true}', SESSION))
        connection.commit(); connection.close()
        with self.assertRaisesRegex(ValueError, "SQLite session state changed"):
            apply_rollout(plan, plan["plan_sha256"], self.root / "run")

    def test_online_backup_projection_repair_and_invariants(self) -> None:
        self.db.unlink()
        make_db(self.db, self.rollout, stale=True)
        before = session_state(self.db, SESSION, self.rollout)
        self.assertEqual(len(before["offset_mismatches"]), 1)
        backup = self.root / "backup.sqlite"
        online_backup(self.db, backup)
        backup_connection = sqlite3.connect(backup)
        self.assertEqual(backup_connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        backup_connection.close()
        changed = repair_offsets(self.db, SESSION, before["offset_mismatches"])
        after = session_state(self.db, SESSION, self.rollout)
        self.assertEqual(changed, 1)
        self.assertEqual(after["offset_mismatches"], [])
        self.assertEqual(before["items_hash"], after["items_hash"])
        self.assertEqual(before["projection"], after["projection"])
        restored = rollback_offsets(self.db, SESSION, before["offset_mismatches"])
        rolled_back = session_state(self.db, SESSION, self.rollout)
        self.assertEqual(restored, 1)
        self.assertEqual(rolled_back["offset_mismatches"], before["offset_mismatches"])

    def test_fresh_process_gate(self) -> None:
        scan_path = self.root / "scan.json"
        write_json(scan_path, self.scan_report())
        plan = create_plan(scan_path, OPERATIONS)
        apply_rollout(plan, plan["plan_sha256"], self.root / "run")
        evidence = {"phase":"final","old_process_uuid":"pid-old","new_process_uuid":"pid-new","thread_resume_success":True,"reconstruction_success":True,"remote_compact_success":True,"assistant_reply_success":True,"compatibility_warnings":[],"projection_warnings":[]}
        gate = record_verification(plan, evidence)
        self.assertTrue(gate["gate_sha256"])
        self.rollout.write_bytes(self.rollout.read_bytes() + b'{"timestamp":"2026-01-01T00:00:10Z","type":"token_usage_record","payload":{}}\n')
        appended_gate = record_verification(plan, evidence)
        self.assertGreater(appended_gate["source_state"]["bytes"], plan["source_post"]["bytes"])
        bad = dict(evidence)
        bad["new_process_uuid"] = "pid-old"
        with self.assertRaisesRegex(ValueError, "process identity"):
            record_verification(plan, bad)

    def test_projection_plan_must_exactly_match_scan(self) -> None:
        self.db.unlink(); make_db(self.db, self.rollout, stale=True)
        report = self.scan_report()
        scan_path = self.root / "scan.json"; write_json(scan_path, report)
        operations_path = self.root / "projection-operations.json"
        write_json(operations_path, report["sqlite_state"]["offset_mismatches"])
        plan = create_plan(scan_path, operations_path)
        self.assertEqual(plan["stage"], "projection")
        self.assertEqual(plan["source_pre"], plan["source_post"])

    def test_discovery_reports_multiple_segments_without_selecting(self) -> None:
        sessions = self.root / "codex-home" / "sessions" / "2026" / "01" / "01"
        sessions.mkdir(parents=True)
        original = FIXTURE.read_text(encoding="utf-8")
        (sessions / "rollout-a.jsonl").write_text(original, encoding="utf-8")
        (sessions / "rollout-b.jsonl").write_text(original.replace("synthetic-session-0001", "synthetic-session-0002"), encoding="utf-8")
        matches = discover(self.root / "codex-home", "synthetic-thread-0001", None)
        self.assertEqual(len(matches), 2)

    def test_parent_cwd_matches_are_candidate_only_and_cannot_enter_plan(self) -> None:
        sessions = self.root / "codex-home" / "sessions" / "2026" / "01" / "01"
        sessions.mkdir(parents=True)
        parent_rollout = sessions / "rollout-parent.jsonl"
        original = FIXTURE.read_text(encoding="utf-8").replace(r"C:\\synthetic\\study", r"C:\\synthetic")
        parent_rollout.write_text(original, encoding="utf-8")
        matches = discover(self.root / "codex-home", None, r"C:\synthetic\study")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["match_kind"], "parent-cwd-candidate")
        self.assertEqual(matches[0]["selection_status"], "candidate-only")
        report = self.scan_report()
        report["candidate"]["selection_status"] = "candidate-only"
        scan_path = self.root / "candidate-scan.json"
        write_json(scan_path, report)
        with self.assertRaisesRegex(ValueError, "candidate-only"):
            create_plan(scan_path, OPERATIONS)

    def test_custom_tool_ids_are_discovered_but_block_writes(self) -> None:
        custom = self.root / "custom.jsonl"
        data = FIXTURE.read_text(encoding="utf-8")
        data += '{"timestamp":"2026-01-01T00:00:11Z","type":"response_item","payload":{"type":"custom_tool_call","id":"ctc_call_synthetic","call_id":"keep","name":"tool","input":"{}"}}\n'
        data += '{"timestamp":"2026-01-01T00:00:12Z","type":"response_item","payload":{"type":"custom_tool_call_output","id":"ctco_synthetic","call_id":"keep","output":"ok"}}\n'
        custom.write_text(data, encoding="utf-8")
        report = scan_rollout(custom)
        self.assertEqual(len(report["unverified_item_findings"]), 2)
        self.assertTrue(all(item["classification"] == "discovered-unverified" for item in report["unverified_item_findings"]))
        self.assertTrue(all(item["deletion_allowed"] is False for item in report["unverified_item_findings"]))
        self.assertFalse(report["schema_guard"]["known"])

    def test_event_item_mirrors_do_not_count_as_semantic_references(self) -> None:
        mirrored = self.root / "mirrored.jsonl"
        records = [json.loads(line) for line in FIXTURE.read_bytes().splitlines()]
        item_id = records[4]["payload"]["id"]
        records.insert(4, {"timestamp":"2026-01-01T00:00:03.5Z","type":"event_msg","payload":{"type":"item_completed","item":{"id":item_id}}})
        mirrored.write_text("\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n", encoding="utf-8")
        report = scan_rollout(mirrored)
        finding = next(item for item in report["item_findings"] if item["id"] == item_id)
        self.assertTrue(finding["mirror_references"])
        self.assertEqual(finding["semantic_references"], [])
        self.assertTrue(finding["safe_to_remove_transport_id"])
        operation = [{"kind":"delete","line":6,"path":["payload","id"],"expected":item_id}]
        candidate = apply_operations_to_bytes(mirrored, operation, report["item_findings"])
        candidate_records = [json.loads(line) for line in candidate.splitlines()]
        self.assertEqual(candidate_records[4]["payload"]["item"]["id"], item_id)
        self.assertNotIn("id", candidate_records[5]["payload"])

    def test_verified_legacy_setting_paths_patch_structurally(self) -> None:
        rollout = self.root / "legacy-settings.jsonl"
        shutil.copy2(PRODUCER_FIXTURES["0.155.0-alpha.16.4"], rollout)
        report = scan_rollout(rollout)
        operations = [
            {"kind":"set","line":1,"path":["payload","model_provider"],"expected":"LegacyProvider","value":"openai"},
            {"kind":"set","line":2,"path":["payload","model"],"expected":"legacy-model","value":"official-model"},
            {"kind":"set","line":3,"path":["payload","thread_settings","model"],"expected":"legacy-model","value":"official-model"},
            {"kind":"set","line":3,"path":["payload","thread_settings","model_provider_id"],"expected":"LegacyProvider","value":"openai"},
            {"kind":"set","line":3,"path":["payload","thread_settings","collaboration_mode","settings","model"],"expected":"legacy-model","value":"official-model"},
        ]
        candidate = apply_operations_to_bytes(rollout, operations, report["item_findings"])
        records = [json.loads(line) for line in candidate.splitlines()]
        self.assertEqual(records[0]["payload"]["model_provider"], "openai")
        self.assertEqual(records[1]["payload"]["model"], "official-model")
        self.assertEqual(records[2]["payload"]["thread_settings"]["model_provider_id"], "openai")
        self.assertEqual(records[3]["payload"]["content"][0]["text"], "Synthetic body")

    def test_plan_records_explicit_target(self) -> None:
        scan_path = self.root / "scan.json"
        write_json(scan_path, self.scan_report())
        document = {"target":{"provider":"openai","model":"official-model","reasoning":"medium"},"operations":json.loads(OPERATIONS.read_text(encoding="utf-8"))}
        operations_path = self.root / "targeted-operations.json"
        write_json(operations_path, document)
        plan = create_plan(scan_path, operations_path)
        self.assertEqual(plan["migration_target"], document["target"])

    def test_synthetic_cli_scan(self) -> None:
        report = self.root / "cli-scan.json"
        command = [sys.executable, str(ROOT / "scripts" / "ccswitch_migrator.py"), "scan", "--rollout", str(self.rollout), "--thread-id", "synthetic-thread-0001", "--session-id", SESSION, "--sqlite", str(self.db), "--codex-version", "codex-cli 0.160.0", "--output", str(report)]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["write_allowed"])

    def test_apply_and_guarded_rollback_preserve_protected_files(self) -> None:
        auth_before = hashlib.sha256((self.root / "auth.json").read_bytes()).hexdigest()
        config_before = hashlib.sha256((self.root / "config.toml").read_bytes()).hexdigest()
        scan_path = self.root / "scan.json"
        write_json(scan_path, self.scan_report())
        plan = create_plan(scan_path, OPERATIONS)
        result = apply_rollout(plan, plan["plan_sha256"], self.root / "run")
        journal = json.loads(Path(result["journal"]).read_text(encoding="utf-8"))
        restored = rollback_rollout(journal)
        self.assertEqual(restored["restored_sha256"], plan["source_pre"]["sha256"])
        self.assertEqual(hashlib.sha256((self.root / "auth.json").read_bytes()).hexdigest(), auth_before)
        self.assertEqual(hashlib.sha256((self.root / "config.toml").read_bytes()).hexdigest(), config_before)

    def test_bootstrap_state_and_catalog_migrate_and_guarded_rollback(self) -> None:
        plan = self.bootstrap_plan()
        result = apply_bootstrap_plan(plan, plan["plan_sha256"], self.root / "bootstrap-run")
        after = scan_bootstrap_metadata(self.state_db, self.catalog_db, "synthetic-thread-bootstrap")
        self.assertEqual(after["state"]["row"]["model_provider"], "openai")
        self.assertEqual(after["state"]["row"]["model"], "official-model")
        self.assertEqual(after["state"]["row"]["reasoning_effort"], "medium")
        self.assertEqual(after["catalog"]["row"]["model_provider"], "openai")
        rollback_bootstrap(Path(result["journal"]))
        restored = scan_bootstrap_metadata(self.state_db, self.catalog_db, "synthetic-thread-bootstrap")
        self.assertEqual(restored["state"]["row"]["model_provider"], "OpenAI")
        self.assertEqual(restored["catalog"]["row"]["model_provider"], "OpenAI")

    def test_bootstrap_first_database_state_drift_stops(self) -> None:
        plan = self.bootstrap_plan()
        connection = sqlite3.connect(self.state_db); connection.execute("UPDATE threads SET updated_at=updated_at+1"); connection.commit(); connection.close()
        # Non-canonical fields are intentionally outside the target row hash.
        connection = sqlite3.connect(self.state_db); connection.execute("UPDATE threads SET model='drifted'"); connection.commit(); connection.close()
        with self.assertRaisesRegex(ValueError, "state changed"):
            apply_bootstrap_plan(plan, plan["plan_sha256"], self.root / "run")

    def test_bootstrap_second_database_state_drift_stops(self) -> None:
        plan = self.bootstrap_plan()
        connection = sqlite3.connect(self.catalog_db); connection.execute("UPDATE local_thread_catalog SET model_provider='drifted'"); connection.commit(); connection.close()
        with self.assertRaisesRegex(ValueError, "state changed"):
            apply_bootstrap_plan(plan, plan["plan_sha256"], self.root / "run")

    def test_bootstrap_second_write_failure_compensates_first(self) -> None:
        plan = self.bootstrap_plan()
        def fail(phase: str) -> None:
            if phase == "before-catalog": raise RuntimeError("synthetic catalog failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic catalog failure"):
            apply_bootstrap_plan(plan, plan["plan_sha256"], self.root / "run", fail)
        restored = scan_bootstrap_metadata(self.state_db, self.catalog_db, "synthetic-thread-bootstrap")
        self.assertEqual(restored["state"]["row"]["model_provider"], "OpenAI")
        journal = json.loads((self.root / "run" / "bootstrap-change-journal.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "compensated")

    def test_bootstrap_compensation_failure_is_high_risk(self) -> None:
        plan = self.bootstrap_plan()
        def fail(phase: str) -> None:
            if phase in {"before-catalog", "before-compensation"}: raise RuntimeError(phase)
        with self.assertRaisesRegex(RuntimeError, "HIGH-RISK"):
            apply_bootstrap_plan(plan, plan["plan_sha256"], self.root / "run", fail)
        journal = json.loads((self.root / "run" / "bootstrap-change-journal.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "high-risk-compensation-failed")

    def test_guarded_update_rejects_zero_and_multiple_rows(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE sample(id TEXT,value TEXT)")
        connection.executemany("INSERT INTO sample VALUES(?,?)", [("same","old"),("same","old")])
        with self.assertRaisesRegex(RuntimeError, "got 0"):
            _guarded_update(connection, "sample", {"id":"missing"}, {"value":"old"}, {"value":"new"})
        with self.assertRaisesRegex(RuntimeError, "got 2"):
            _guarded_update(connection, "sample", {"id":"same"}, {"value":"old"}, {"value":"new"})

    def test_bootstrap_schema_mismatch_is_scan_only(self) -> None:
        connection = sqlite3.connect(self.state_db); connection.execute("ALTER TABLE threads ADD COLUMN future_column TEXT"); connection.commit(); connection.close()
        report = scan_bootstrap_metadata(self.state_db, self.catalog_db, "synthetic-thread-bootstrap")
        self.assertFalse(report["write_allowed"])
        self.assertFalse(report["state"]["schema_guard"]["known"])

    def test_bootstrap_target_row_hash_mismatch_invalidates_plan(self) -> None:
        plan = self.bootstrap_plan()
        connection = sqlite3.connect(self.catalog_db); connection.execute("UPDATE local_thread_catalog SET cwd='C:/synthetic/changed'"); connection.commit(); connection.close()
        with self.assertRaisesRegex(ValueError, "state changed"):
            apply_bootstrap_plan(plan, plan["plan_sha256"], self.root / "run")

    def test_turn_context_nested_collaboration_only_latest_is_patchable(self) -> None:
        rollout = self.root / "nested.jsonl"
        records = [
            {"type":"session_meta","payload":{"cli_version":"0.160.0","model_provider":"openai"}},
            {"type":"turn_context","payload":{"model":"official-model","collaboration_mode":{"settings":{"model":"legacy-old","reasoning_effort":"medium"}}}},
            {"type":"turn_context","payload":{"model":"official-model","collaboration_mode":{"settings":{"model":"legacy-current","reasoning_effort":"medium"}}}},
            {"type":"world_state","payload":{"settings":{"model":"legacy-world"}}},
        ]
        rollout.write_text("\n".join(json.dumps(item,separators=(",",":")) for item in records)+"\n", encoding="utf-8")
        report = scan_rollout(rollout)
        nested = [item for item in report["settings"] if item["event_type"] == "turn_context" and item["path"] == ["payload","collaboration_mode","settings","model"]]
        self.assertEqual([item["is_effective"] for item in nested], [False, True])
        operation = [{"kind":"set","line":3,"path":["payload","collaboration_mode","settings","model"],"expected":"legacy-current","value":"official-model"}]
        candidate = apply_operations_to_bytes(rollout, operation, report["item_findings"], report["settings"])
        patched = [json.loads(line) for line in candidate.splitlines()]
        self.assertEqual(patched[1]["payload"]["collaboration_mode"]["settings"]["model"], "legacy-old")
        self.assertEqual(patched[2]["payload"]["collaboration_mode"]["settings"]["model"], "official-model")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            apply_operations_to_bytes(rollout, [{**operation[0],"line":2,"expected":"legacy-old"}], report["item_findings"], report["settings"])

    def test_world_state_is_report_only_and_cannot_patch(self) -> None:
        rollout = self.root / "world.jsonl"
        rollout.write_text('{"type":"session_meta","payload":{"cli_version":"0.160.0","model_provider":"openai"}}\n{"type":"world_state","payload":{"settings":{"model":"legacy-world"}}}\n', encoding="utf-8")
        report = scan_rollout(rollout)
        world = next(item for item in report["settings"] if item["event_type"] == "world_state")
        self.assertTrue(world["report_only"])
        with self.assertRaisesRegex(ValueError, "unsupported"):
            apply_operations_to_bytes(rollout, [{"kind":"set","line":2,"path":["payload","settings","model"],"expected":"legacy-world","value":"official"}], [], report["settings"])

    def test_bootstrap_never_touches_auth_or_config(self) -> None:
        auth = hashlib.sha256((self.root / "auth.json").read_bytes()).hexdigest()
        config = hashlib.sha256((self.root / "config.toml").read_bytes()).hexdigest()
        plan = self.bootstrap_plan(); apply_bootstrap_plan(plan, plan["plan_sha256"], self.root / "run")
        self.assertEqual(hashlib.sha256((self.root / "auth.json").read_bytes()).hexdigest(), auth)
        self.assertEqual(hashlib.sha256((self.root / "config.toml").read_bytes()).hexdigest(), config)


if __name__ == "__main__":
    unittest.main()
