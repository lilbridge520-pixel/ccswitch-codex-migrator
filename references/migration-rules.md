# Compatibility rules

These rules summarize observed legacy-provider failures without binding migration decisions to a specific historical thread.

## Provider and model

Legacy threads can retain provider/model settings independently of the current global configuration. Scan `thread_settings_applied` and `turn_context` records, especially after the latest effective compact checkpoint. A provider spelling that resembles an official provider is not proof of equivalence. The target provider/model must be explicit in the migration plan and supported by the current official account.

Legacy producers may serialize settings as `event_msg.payload.thread_settings` and may keep the provider in `session_meta.payload.model_provider`. These paths are eligible only when their exact producer schema fingerprint is write-compatible. A migration plan records provider, model, and reasoning as explicit target metadata even when one of those values is already correct and therefore needs no mutation.

Resume can fail before rollout reconstruction because bootstrap metadata is also persisted in `state_5.sqlite/threads` and `sqlite/codex-dev.db/local_thread_catalog`. Scan both exact target rows. A bootstrap plan uses exact old-value predicates, exact schema and canonical row hashes, and requires one updated row per database. Commit `state_5.sqlite` first, then the catalog; if the second commit fails, perform a guarded reverse update on the first and stop.

Scan `turn_context.payload.collaboration_mode.settings.model`, `.reasoning_effort`, and any present provider field. Only the latest effective turn context may produce a patch operation. Older values remain evidence only. `world_state` settings are report-only in version 1.

Known fixture examples include `OpenAI`, `qwen3.7-plus`, and `gpt-6.1-sol`. They are test vocabulary only. Never select a replacement model from these literals.

## Response item IDs

Known legacy fixtures include `resp_chatcmpl-*`, `rs_resp_chatcmpl-*`, and `fc_call_*`. Prefix alone is not an edit decision. Classify an ID using all of:

- event type and payload type;
- exact JSON path;
- whether the record is in the active compact window;
- whether another record references it;
- whether it is a transport item ID or a local UI/projection ID.

An unreferenced optional `response_item.payload.id` can be proposed for deletion. Prefer deletion over fabricating an official ID. Preserve `event_msg.item.id`, retained-source `message_id`, function `call_id`, function outputs, and reasoning summaries.

An `event_msg.payload.item.id` that mirrors a `response_item.payload.id` is a preserved local mirror, not a semantic server-item reference. Report it separately. Any other reference remains semantic and blocks ID removal.

`ctc_call_*` and `ctco_*` are discovered-but-unverified custom-tool item families. Scan their locations and references, but do not produce delete, set, or remap operations for them.

## Compact visibility

The latest effective compact checkpoint defines the history likely to be serialized for the next remote compact. Report both all-history findings and active-window findings. Do not rewrite harmless legacy identifiers outside the active window merely because they share a prefix.

## Process state

Editing a durable rollout does not change a history already loaded in memory. Runtime verification requires a new desktop process and a new reconstruction. Repeated old errors from the same process are not evidence that the disk patch failed.
