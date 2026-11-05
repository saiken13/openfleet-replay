# Constraints

These are rules we follow until we deliberately change them. To change one, explain why first, then record the change in [DECISIONS.md](DECISIONS.md).

## Scope and architecture
- Don't introduce microservices during the MVP without strong, written justification.
- Don't add Kafka/NATS/Redis before basic ingestion works *and* a measured need exists (see DECISIONS #009).
- Don't add Kubernetes before Docker Compose works end-to-end.
- Don't add AI features before telemetry, incidents, and replay work.
- Don't add complexity only because it looks more "enterprise."
- Don't introduce unnecessary dependencies. Each new dependency needs a one-line reason in the PR.

## Code
- Routers contain no SQL and no business rules. Repositories contain no business decisions.
- Detection rules stay pure: no DB, network, or wall-clock access (`now` is passed in).
- All timing and thresholds come from settings, never inline constants.
- Liveness uses server time. Robot timestamps are for ordering only.
- Publish realtime notifications only after the DB transaction commits.
- Don't modify unrelated files in a change.
- Don't rewrite working code without a stated reason.
- Don't store secrets in code. Use environment variables, and `.env` is git-ignored.

## API
- All endpoints live under `/api/v1`.
- Don't silently change API contracts. A breaking change needs a DECISIONS entry and a version or migration note.
- Write endpoints called by robots must be idempotent (safe to retry).

## Testing and honesty
- Don't remove or weaken tests to make builds pass.
- Integration tests run against real PostgreSQL, not SQLite (DECISIONS #012).
- Every schema change ships as an Alembic migration that has been tested for both upgrade and downgrade.
- Don't fabricate benchmarks. Performance claims must come with the command and environment that produced them.
