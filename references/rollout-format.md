# Rollout format guard

Version 1 supports the current Codex `0.160.x` JSONL envelope and a small exact list of audited legacy producer profiles. A legacy version match alone is insufficient: every migration-critical record must match an allowlisted structural fingerprint. See [source-compatibility.md](source-compatibility.md).

Known event families are `session_meta`, `thread_settings_applied`, `turn_context`, `response_item`, `event_msg`, `compacted`, `token_usage_record`, and `world_state`.

Every line must be a JSON object with a string `type`. Events that carry mutable migration fields must have an object `payload`. Unknown event families are reported but make the file scan-only.

Line numbers are one-based. Ordinals are derived from `thread_history_projection_state.next_rollout_ordinal - line_count` when the projection points to the current EOF. Without a trustworthy projection, the scanner blocks writes.

Supported rollout mutations are intentionally narrow:

- `set` on `payload.model` or `payload.model_provider` in `turn_context` or `thread_settings_applied`;
- `set` on the audited provider path in `session_meta`;
- `set` on audited model/provider/reasoning paths in `event_msg.payload.thread_settings`, including collaboration-mode settings;
- `delete` on `payload.id` in a `response_item` whose payload type is `message`, `reasoning`, or `function_call` and whose reference graph is safe.

Parent-cwd discovery is candidate generation only. A `candidate-only` match cannot enter `migration-plan.json` until the user selects the exact Thread ID and session metadata confirms it.

Any other path requires a new Skill version and tests.
