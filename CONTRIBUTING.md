# Contributing

Contributions should preserve the single-Thread safety model, exact schema guards, and synthetic-only test fixtures.

Before opening a pull request:

1. Run the complete Python test suite.
2. Run compile and PowerShell wrapper checks.
3. Run the sensitive-data audit.
4. Do not include real Codex databases, rollout files, credentials, or private migration reports.

Unknown producer versions and schemas must remain scan-only until exact fixtures and tests are added.
