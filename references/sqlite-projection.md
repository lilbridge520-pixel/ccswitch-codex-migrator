# SQLite projection

Version 1 recognizes exact schemas for `thread_history_projection_state`, `thread_items`, `thread_turns`, and `thread_realtime_items`. Extra or missing columns make write operations unavailable until the Skill is updated and tested.

For a target session, `next_rollout_byte_offset` must equal the rollout byte length and `next_rollout_ordinal` must be one greater than the final ordinal. A turn start offset must equal the byte start of its `rollout_ordinal`; a turn end offset must equal the byte end of its `rollout_end_ordinal`.

Projection repair is allowed only when a plan contains the exact old and recomputed offsets for specific turn IDs. The transaction must use guarded updates, preserve every non-offset column, keep `thread_items`, projection state, and realtime items unchanged, and finish with `PRAGMA quick_check = ok`.

Use SQLite online backup before the transaction. Do not routinely restore the whole database after other threads have advanced; use guarded reverse updates from the journal instead.
