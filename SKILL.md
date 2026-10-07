---
name: ccswitch-codex-migrator
description: Safely inspect and migrate one Codex history thread created through CC Switch or another legacy provider. Use for dry-run discovery, rollout compatibility repair, projection-offset repair, rollback, and fresh-process verification. Never use it for batch migration or global Codex configuration changes.
---

# CC Switch Codex Migrator

Migrate exactly one legacy Codex thread through auditable stages. Start read-only. Never interpret a request to inspect one thread as authority to modify it.

## Non-negotiable boundaries

- Never read, write, copy, rename, or delete `auth.json`.
- Version 1 never writes `config.toml`.
- Never batch-migrate. One invocation may target only one Thread ID and one stage plan.
- Never modify message body content, `event_msg.item.id`, retained-source `message_id`, `call_id`, or function outputs unless a future explicitly reviewed rule permits it.
- Never use global string replacement. Parse every JSONL record and mutate an exact JSON field path.
- Unknown Codex version, JSONL event schema, or SQLite schema is scan-only. Do not override this guard.
- Stop at the first failed assertion. Do not widen the edit set or continue to another thread.

Read [references/safety-policy.md](references/safety-policy.md) before any write. Read [references/source-compatibility.md](references/source-compatibility.md) when a rollout was produced by an older Codex version. Read [references/migration-rules.md](references/migration-rules.md) when classifying provider/model or legacy IDs. Read [references/sqlite-projection.md](references/sqlite-projection.md) before projection repair. Read [references/verification-playbook.md](references/verification-playbook.md) before runtime verification.

## Required workflow

1. Run `scan` by exact Thread ID or cwd. A recorded parent cwd may produce `candidate-only` matches; confirm one by title, Thread ID, and session metadata before planning. If multiple candidates match, stop and ask the user to select one.
2. Confirm title/cwd/session metadata without exposing message bodies. Review the compact-window boundary and ID reference graph.
3. Audit the exact producer version and critical JSONL/SQLite schema fingerprints.
4. Scan and plan `thread-bootstrap-metadata` for the exact row in `state_5.sqlite/threads` and `sqlite/codex-dev.db/local_thread_catalog`. This stage must complete before fresh resume.
5. Plan rollout settings migration, then compact-window legacy transport-ID cleanup. Only the latest effective `turn_context.payload.collaboration_mode.settings.*` is writable; historical contexts are report-only. `world_state` is always report-only.
6. Create one stage-specific immutable plan. Record and present its SHA-256. A plan binds rollout SHA-256, byte length, last ordinal, version/schema guards, bootstrap row hashes, and target projection state.
7. Obtain explicit approval for that exact plan SHA before applying it.
8. Back up rollout files byte-for-byte. Back up each SQLite database through the online backup API. Revalidate all bound state immediately before writing.
9. Apply only exact JSON paths or guarded SQL rows. Validate structural diffs, JSON parsing, SQLite integrity, row counts, and invariants.
10. After rollout edits, require a new Codex Desktop process. Fresh `thread/resume` and reconstruction are a formal gate.
11. Only after the fresh-process gate, test remote compact and one harmless assistant reply.
12. Audit projection state. If offsets remain inconsistent, create a separate minimal projection plan; never fold it into an earlier stage.

## Commands

Use the PowerShell wrapper on Windows:

```powershell
./scripts/Invoke-CcSwitchMigrator.ps1 scan --codex-home <path> --thread-id <id> --sqlite <projection-db> --state-db <state_5.sqlite> --catalog-db <codex-dev.db> --output <report.json>
./scripts/Invoke-CcSwitchMigrator.ps1 plan-bootstrap --scan <report.json> --target <target.json> --output <bootstrap-plan.json>
./scripts/Invoke-CcSwitchMigrator.ps1 apply-bootstrap --plan <bootstrap-plan.json> --confirm-plan-sha <sha> --output-dir <run-dir>
./scripts/Invoke-CcSwitchMigrator.ps1 rollback-bootstrap --journal <bootstrap-change-journal.json>
./scripts/Invoke-CcSwitchMigrator.ps1 plan --scan <report.json> --operations <operations.json> --output <migration-plan.json>
./scripts/Invoke-CcSwitchMigrator.ps1 apply-rollout --plan <migration-plan.json> --confirm-plan-sha <sha> --output-dir <run-dir>
./scripts/Invoke-CcSwitchMigrator.ps1 record-verification --plan <plan.json> --evidence <evidence.json> --output <gate.json>
./scripts/Invoke-CcSwitchMigrator.ps1 repair-projection --plan <plan.json> --fresh-gate <gate.json> --confirm-plan-sha <sha> --output-dir <run-dir>
./scripts/Invoke-CcSwitchMigrator.ps1 rollback-rollout --journal <journal.json>
./scripts/Invoke-CcSwitchMigrator.ps1 rollback-projection --journal <journal.json>
```

`scan` is the only command allowed when a guard is unknown. The Python entrypoint supports the same arguments.

## Decision rules

- Treat known legacy model and ID formats as evidence, never as sufficient authority to edit.
- Treat `state_5.sqlite/threads` as resume bootstrap metadata and `local_thread_catalog` as a separate catalog projection. Both must match the immutable plan. Update them independently with compensation if the second commit fails; never use whole-database replacement as normal rollback.
- Exact legacy producer versions are write-compatible only when every migration-critical record matches its audited schema fingerprint.
- Derive edits from the parsed event type, exact field path, compact visibility, and reference graph.
- Prefer removing an optional transport ID when it is unreferenced. Never synthesize an official service ID.
- Preserve reasoning summaries and tool-call linkage.
- Classify `ctc_call_*` and `ctco_*` as discovered-but-unverified. Build their reference graph, but never generate a mutation for them.
- Prefer repairing a few verified `thread_turns` offsets over rebuilding a healthy projection.
- If a target is active in the current desktop process, do not fake a fresh resume. Wait for idle, restart normally, and prove that the process identity changed.

Runtime reports and backups may contain local paths or IDs; store them in a user-approved run directory, never inside this Skill.
