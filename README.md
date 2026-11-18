# CHA Final

A fresh start for the custom Hermes memory plugin. The first milestone is to save one fact explicitly, recall it in a fresh Hermes session, and surface a named failure when the test store is unavailable.

- [AGENTS.md](AGENTS.md) is the entry point for coding agents.
- [CONTRIBUTING.md](CONTRIBUTING.md) governs changes, reviews, and failure handling.
- [ARCHITECTURE.md](ARCHITECTURE.md) records the design and marks capabilities that still need to be built and verified.
- [OPERATIONS.md](OPERATIONS.md) contains deployment requirements and the previous build's runbook for reference.

## Current scope

The first runtime milestone implements explicit save and lexical recall through Hermes v0.23.1. It uses the isolated `cha_final` schema and the `CHA_FINAL_DATABASE_URL` secret in a disposable Hermes profile. Automatic recall and the `memory_search` tool both use the active repository's Git origin. Projectless workspaces read unscoped facts. An outage yields a named failure and a local operator log.

CI runs against disposable PostgreSQL 17 with the pinned Hermes release. Local verification uses the same Hermes version and a fresh PostgreSQL instance named by `CHA_FINAL_TEST_DATABASE_URL`. Run `python -B -m unittest discover -s tests -v` with Git and Ruby available and a dedicated temporary `HERMES_HOME`.

Use the [legacy memory rehearsal](OPERATIONS.md#legacy-memory-rehearsal) to test importing live facts from a fixed legacy snapshot.
