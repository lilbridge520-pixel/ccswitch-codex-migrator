# Source rollout compatibility

Write compatibility is an intersection of the current runtime guard, the exact producer version, critical JSONL structure fingerprints, and the SQLite schema guard. Never promote a version family by wildcard.

## Audited exact producer profiles

- `0.155.0-alpha.9.2`
- `0.155.0-alpha.16.4`
- `0.158.0-alpha.2.1`

For these profiles, the guard fingerprints migration-critical records using event identity plus field names and JSON value types. It checks:

- `session_meta`, including the provider path;
- `turn_context`, including model and reasoning fields;
- top-level or `event_msg` thread-settings structures;
- `response_item` message, reasoning, and function-call records that carry an `id`;
- effective compact/checkpoint records when present.

The exact accepted fingerprints live in `scripts/migrator/version_guard.py` and are exercised by completely synthetic fixtures. A producer version with an unseen critical fingerprint remains scan-only. A profile that has no accepted compact fingerprint remains write-compatible only for rollouts without such a checkpoint.

## Official runtime append boundary

An old producer rollout can later receive official current-runtime records after a successful remote compact. Version 1 recognizes only the audited `compacted`, `turn_context`, and `response_item/message` fingerprints listed in `OFFICIAL_RUNTIME_APPEND_FINGERPRINTS`. They are accepted only at or after an exact audited `compacted` boundary in the same rollout; they do not broaden the legacy producer profile or any `0.160.*` family. Any unknown post-boundary fingerprint remains scan-only.

## Custom-tool hold

`ctc_call_*` and `ctco_*` are intentionally not write-compatible. Their presence is reported with locations and references and makes the rollout scan-only. No operation validator accepts their paths.
