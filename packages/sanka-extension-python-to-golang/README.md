# Python to Golang (experimental)

Extension ID: `sanka/python-to-golang`. Sources: DRF, FastAPI, Flask.
Targets: Fiber (default), chi, Gorilla mux, Gin.

This first increment produces a Go `backend` package exposing `NewApp` for
literal public JSON GET endpoints. It is **not a complete backend migration**,
not published in the extension catalog, and not qualified for production cutover.
The full backend implementation remains in progress.

Use the existing extension JSON subprocess protocol via
`sanka-extension-python-to-golang`, with configuration:

```json
{"source_framework":"flask","target_framework":"fiber","source_file":"app.py","database_layer":"none"}
```

`scan` reports unsupported constructs. `plan` contains deterministic generated
files and a `plan_hash`. `apply` requires the runtime review attestation (`reviewed_plan_hash`) and that
hash as `extension_plan_hash`,
recomputes the plan from the current source and configuration, and refuses changed
plans or existing output. Artifacts must be inside the project's `.sanka`
directory; output is `golang/` within that artifact directory.

`scan`, `plan`, and `apply` parse source without importing or executing it. The current capture accepts
one top-level endpoint module (plus the explicitly selected models module for pgx)
and rejects unknown imports, decorators, configuration,
request parameters, dynamic handlers, additional Python modules, and source
symlinks. DRF endpoints require explicit public permission, no authentication,
and JSON renderer declarations. Unsupported behavior blocks generation.

Generated Go dependencies and checksums are pinned in this package; Go 1.26.5 is
the qualification toolchain. No Go dependencies are installed into Sanka's Python
environment. No services or repositories are generated for stateless handlers.

The current integration matrix compares successful JSON GET responses with the
actual Python framework clients across all twelve source/target combinations.
It does not establish default errors, HEAD/OPTIONS, redirects, middleware,
content negotiation, deployment, or whole-backend parity.

`test` runs Go tests and exercises every captured GET route in a temporary copy of
its generated package. `verify` additionally executes the captured Python module
with its framework test client and compares status, parsed JSON body, and media
type. Both require an unchanged saved plan, generated output, and Go 1.26.5 on
PATH. The extension interpreter needs the source framework and its test-client
dependencies installed. Go may download the checksum-pinned dependencies.

Replay executes source and candidate code; the temporary directory is not a
security sandbox. It does not start listening servers or alter candidate files.
For DRF, the qualified fixture settings use no installed apps and no unauthenticated
user model; external application settings and middleware are not captured.
Reports include source and candidate digests. Manual handler repairs are tested,
but changes to Go locks or the captured contract are rejected. A mismatch returns
an error with a report path. Each rerun invalidates its old report first, so a
failed rerun cannot leave stale passing evidence.

## PostgreSQL schema profile

Select `database_layer: "pgx"` to generate a PostgreSQL baseline, Go model structs,
and an embedded Goose migration command. The resolved configuration pins
`database_dialect: "postgresql"`, `migration_tool: "goose"`, `schema_mode: "empty"`,
and `models_file: "models.py"`. Other database/tool combinations are rejected.

DRF models must directly inherit `models.Model` and declare `Meta.app_label`,
`Meta.db_table`, and an explicit primary key. Flask/FastAPI models currently use
SQLAlchemy 2 `DeclarativeBase`, `Mapped`, and `mapped_column` declarations in the
selected models file. Models are captured statically; they are never imported by
scan/plan/apply.

Qualified schema fields are 32/64-bit integers, booleans, bounded strings, and
text, with nullability, single integer primary keys, automatic IDs and single
column uniqueness. Nullable Go fields use pointers. Defaults (including Python
and server defaults), relationships, indexes, custom types, validators, managers,
and model methods remain blockers. Generated structs do not implement validation.

After reviewing the generated SQL, set `DATABASE_URL` and run
`go run ./cmd/migrate up` from the generated directory. Migrations are embedded,
transactional and protected by a PostgreSQL session advisory lock. The initial
migration rejects existing application relations; it never adopts an existing
schema or transfers data. Reapplying an applied baseline is a no-op.
`go run ./cmd/migrate down` explicitly drops the generated tables and their data.
Do not edit an applied baseline; later schema changes require new revisions.

This profile does not yet migrate database-backed HTTP handlers or generate
repositories/services. The current public `test`/`verify` commands still exercise
only the captured GET contract; PostgreSQL schema, constraint, apply/reapply and
rollback equivalence are checked separately by the opt-in integration suite.

## Remaining backend work

1. Capture whole projects into a framework-neutral contract, with stable model,
   field, relationship, operation, validation and policy identities.
2. Extend the initial pgx/Goose profile to richer schemas, data migration and
   transactional CRUD. Qualify each supported combination against a real database before
   advertising it; reject incompatible combinations.
3. Preserve validation/errors, auth/permissions, middleware, filtering,
   pagination, transactions and configuration. Introduce services only for
   captured business orchestration and repositories only for persistence needs.
4. Extend source/Go replay through the public lifecycle to cover failure paths,
   database effects, rollback, generated deployment entrypoints and shutdown.
5. Package/install verification and CI must pass before catalog publication.

Do not infer support from a framework appearing in the target choices: supported
behavior is defined by the capture gaps and independently exercised contracts.
