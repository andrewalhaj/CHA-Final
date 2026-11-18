# CHA Final

A fresh start for the custom Hermes memory plugin. The first milestone is to save one fact explicitly, recall it in a fresh Hermes session, and surface a named failure when the test store is unavailable.

- [AGENTS.md](AGENTS.md) is the entry point for coding agents.
- [CONTRIBUTING.md](CONTRIBUTING.md) governs changes, reviews, and failure handling.
- [ARCHITECTURE.md](ARCHITECTURE.md) records the design and marks capabilities that still need to be built and verified.
- [OPERATIONS.md](OPERATIONS.md) contains deployment requirements and the previous build's runbook for reference.

This bootstrap contains documentation and repository checks. Run the checks with Git and Ruby available using `python -B -m unittest discover -s tests -v`.
