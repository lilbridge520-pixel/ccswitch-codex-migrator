# Safety policy

## Write authorization

A scan is not authorization to migrate. Before a write, show the user the target Thread ID, rollout/session, planned operations, backup location, plan SHA-256, and guard result. Continue only after approval of that exact plan.

## Protected data

`auth.json` is outside the workflow. Do not open it, hash its contents, copy it, or include it in reports. Version 1 also never writes `config.toml`; semantic inspection is allowed only when the user asks for it separately.

Do not store real messages, credentials, account identifiers, usernames, or machine-specific paths in Skill sources or test fixtures. Runtime reports should omit message body text and use hashes where identity confirmation is needed.

## Stop conditions

Stop without writing when any of these occurs:

- more than one thread matches;
- Codex version is unsupported;
- JSONL or SQLite schema is unknown;
- any JSONL record fails parsing;
- rollout or database state differs from the plan;
- a planned field's current value differs from `expected`;
- a supposedly removable ID has an unexplained reference;
- backup or integrity check fails;
- target thread is active during a required fresh-process boundary;
- the guarded SQL update count differs from the plan;
- runtime verification returns a new compatibility error.

## Backup and rollback

Rollout backups are byte-for-byte copies created before replacement. SQLite backups use `sqlite3.Connection.backup`, which includes a consistent view of WAL state. A committed rollback is guarded by the current post-migration hash; never overwrite a rollout that has received later appends.

For bootstrap metadata, create independent online backups for both databases and a multi-database journal containing schema hash, canonical target-row hash, old/new fields, and reverse guards. SQLite files cannot share an atomic transaction: commit `state_5.sqlite` first and the local catalog second. A second-database failure triggers a guarded compensation update on the first. Compensation failure is high-risk: stop, preserve the journal and backups, and give disaster-recovery guidance. Do not restore a whole database as the normal rollback path.
