# CC Switch Codex Migrator

Safely migrate legacy Codex threads created with CC Switch or another custom provider to the official OpenAI Codex runtime.

> **v0.1.0-beta / Experimental** — unofficial community software. It is not affiliated with or endorsed by OpenAI or CC Switch.

## What it does

The migrator inspects one local Codex history thread at a time, identifies legacy provider/model metadata and transport identifiers, creates an immutable migration plan, and applies only guarded, field-level changes after explicit approval. It also audits SQLite projection offsets and can repair a verified target session.

## Why it exists

Legacy threads may fail with errors such as `Model provider 'OpenAI' not found`, unsupported legacy models, `resp_chatcmpl-*` or `rs_resp_chatcmpl-*` identifiers, remote compact 400/404 responses, or SQLite projection offset drift after a rollout is edited.

## Safety philosophy

- Dry-run first; one Thread ID per operation.
- Immutable plans bind source hashes, lengths, ordinals, schema fingerprints, and database row state.
- Backups are created before writes.
- JSONL is parsed and patched by exact field path; global string replacement is never used.
- SQLite updates are guarded and transactional, with online backups and rollback journals.
- A fresh Codex Desktop process is required before runtime validation.
- Unknown Codex versions or schemas are scan-only.
- `auth.json` is never read or modified. Version 1 never writes `config.toml`.

## Installation

Copy this directory to `<CODEX_HOME>/skills/ccswitch-codex-migrator` or install it through your local Codex skill manager. Windows PowerShell is the primary supported environment.

## Usage

```powershell
./scripts/Invoke-CcSwitchMigrator.ps1 scan `
  --codex-home <CODEX_HOME> `
  --thread-id <THREAD_ID> `
  --sqlite <CODEX_HOME>/thread_history_1.sqlite `
  --state-db <CODEX_HOME>/state_5.sqlite `
  --catalog-db <CODEX_HOME>/sqlite/codex-dev.db `
  --output <OUTPUT_DIR>/scan-report.json
```

Scanning by cwd is supported by the Python entrypoint. Parent-directory matches are candidate-only until title, Thread ID, and session metadata confirm identity.

Generate and review a dry-run plan before applying it:

```powershell
./scripts/Invoke-CcSwitchMigrator.ps1 plan --scan <OUTPUT_DIR>/scan-report.json --operations <OUTPUT_DIR>/operations.json --output <OUTPUT_DIR>/migration-plan.json
```

Never apply a plan without verifying its SHA-256 and explicitly approving that exact immutable plan.

## Migration stages

1. Discovery and identity confirmation
2. Producer version and schema guards
3. Thread bootstrap metadata (`state_5.sqlite` and `codex-dev.db`)
4. Rollout settings migration
5. Compact-window legacy transport-ID cleanup
6. Static validation and fresh-process gate
7. `thread/resume`, reconstruction, and remote compact validation
8. Projection audit and minimal offset repair when independently planned

## Tested

The beta has passed synthetic unit, safety-invariant, and integration tests, plus two private end-to-end migration validations. Private project names, Thread IDs, paths, messages, and runtime artifacts are intentionally not included here.

## Known limitations

- Codex internal schemas change; unknown fingerprints remain scan-only.
- Batch migration is not supported.
- `config.toml` is never written by version 1.
- `auth.json` is never read or modified.
- Custom-tool legacy identifiers require more compatibility fixtures and real-world validation.
- Validation has primarily been performed on Windows.

## Disclaimer

This is an unofficial community tool and is not affiliated with or endorsed by OpenAI or CC Switch. It may modify local Codex history only after explicit approval. Back up your Codex data before migrating.
