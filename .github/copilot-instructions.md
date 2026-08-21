# Project Guidelines

## Architecture

For new code, refactors, workflow changes, scheduler changes, persistence changes, and filesystem
mutation changes, follow [docs/architecture.md](../docs/architecture.md).

Treat that document as the adopted architectural contract. Keep behavior-preserving structural moves
small and test-backed, preserve public CLI behavior unless explicitly changed, and do not bypass the
central job state model as it is introduced.

When moving behavior, update every caller and delete the replaced path in the same green slice.
Keep only one reachable implementation for each workflow.
