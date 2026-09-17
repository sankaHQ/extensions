# Flask PostgreSQL replay and backend qualification

Continue the approved [backend migration plan](2026-09-16-backend-migrations.md)
from merged PR61, `78b5cce106c4def97bf00374c59ca84067484556`.

## Scope

1. Extend generic replay with an explicit PostgreSQL backend and an environment-name
   reference to a dedicated test maintenance DSN. Seed one newly created database,
   clone it independently for every source/candidate scenario, compare typed rows
   and sequence state, and always clean up only resources created by this run.
2. Forward configuration through both existing extension adapters, preserving SQLite
   defaults. Validate connection binding and keep maintenance credentials out of
   reports, process arguments and application environments. Source code remains
   trusted executable input; this work does not add a sandbox.
3. Qualify generated Flask backends through scan, reviewed plan, apply and verify:
   simple CRUD, authenticated relational operations and transactional nested writes.
   Assert intended source outcomes, negative cases, deterministic output and YAGNI
   layer decisions. Reuse current fixtures before introducing new application code.
4. Run focused checks, bounded full checks and CI PostgreSQL/installed-wheel lanes.
   Version changed distributions, rebuild reproducible candidate artifacts, review
   and open a Change Bot PR. Publication and Go work are separate later gates.

No local servers, customer database writes or full customer migrations. PostgreSQL
integration runs on the existing CI service; local runs skip without the dedicated
service environment. Existing SQLite replay remains a regression gate.

## Initial implementation evidence

- Post-merge CI for PR61 passed at `78b5cce` (run35164500956).
- Composed protocol fixtures pass on SQLite: inventory CRUD, authenticated
  session/CSRF notes and atomic nested orders (20 intended-status scenarios).
  PostgreSQL variants require the service-backed CI lane.
- Composition exposed an explicit Cookie header bug in generic Flask replay.
  The replay client now preserves request-only overrides and cookies set by setup
  responses, matching the source test client.
- Candidate versions: replay a4, Flask a12, FastAPI a17; shared capture helper a3
  unchanged. Candidate release a31 is not a publication.

Local full validation passed: **929 tests, 34 service/platform skips**, one existing
Starlette deprecation warning, plus 23 packaging-script tests. Ruff passed across
324 files; mypy passed across 123 source files. Two canonical builds produced
identical bytes for all 194 wheels, and manifest hashes validate.

Independent review closed a PostgreSQL fixture schema-name assertion and required
server-side ownership markers before cleanup. An ambiguous creation outcome is
reported for manual inspection if ownership cannot be established; it never permits
unconfirmed deletion. Final whole-branch review found no blocker. Cleanup error
messages include generated database names without credentials.

The installed candidate, checked to load the built Flask/replay/helper wheels with
published SDK a4, passed 15 tests with four PostgreSQL skips. The separate Django
6.0.6 / DRF 3.17.1 environment passed all three composed SQLite profiles (three
PostgreSQL skips). PostgreSQL and installed-wheel CI remain final-head gates.
