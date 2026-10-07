# Verification playbook

## Static gate

Confirm zero JSON parse errors, expected structured diffs only, unchanged message bodies, preserved linkage fields, and unchanged protected files. For SQLite, require `quick_check`, exact update counts, legal line boundaries, and an EOF-aligned cursor.

## Fresh-process gate

After rollout modification, record the old Codex process identity, fully restart Codex Desktop, record a different identity, execute `thread/resume`, confirm reconstruction without malformed/stale/projection warnings, and record evidence with `record-verification`. Navigation within the same process does not satisfy this gate.

## Final runtime gate

After fresh reconstruction, run one real remote compact when appropriate and send a harmless prompt requesting a fixed reply without tools or file writes. Success requires completed compact, completed turn, assistant reply, and no new compatibility/projection warnings.

If context size does not naturally trigger compact, report it as not exercised rather than claiming success.
