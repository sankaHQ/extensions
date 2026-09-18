# Python to Golang (experimental)

Extension ID: `sanka/python-to-golang`. Sources: DRF, FastAPI, Flask.
Targets: Fiber (default), chi, Gorilla mux, Gin.

The current implementation produces a Go `backend` package exposing `NewApp` for
literal public JSON GET endpoints, bounded PostgreSQL reads, and the first qualified
create/PATCH/DELETE profile. Every target includes a runnable `cmd/api`. It is **not a complete backend migration**,
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
request parameters, unsupported dynamic handlers, additional Python modules, and source
symlinks. DRF endpoints require explicit public permission, no authentication,
and JSON renderer declarations. Unsupported behavior blocks generation.

Generated Go dependencies and checksums are pinned in this package; Go 1.26.5 is
the qualification toolchain. No Go dependencies are installed into Sanka's Python
environment. No services or repository interfaces are generated for these direct operations.

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

## Process entrypoint

Every target includes `cmd/api/main.go` and a configuration test. `PORT` defaults to 8080 and
must be an integer from 1 through 65535. Database-backed handlers additionally require
`DATABASE_URL`; startup parses and pings the pgx pool with a ten-second bound. The process owns
and closes that pool. Each server enforces a body limit plus read, write, and idle timeouts; the
standard servers also cap headers. Fiber uses its native shutdown configuration; chi, mux, and Gin
run through a bounded standard `net/http` server. All targets handle `SIGINT` and `SIGTERM`
gracefully.

The API never applies migrations on boot. Review and run `go run ./cmd/migrate up` separately,
then start it with `go run ./cmd/api`. Validate generated projects with:

```sh
go test ./...
go vet ./...
go build ./cmd/api
```

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

PostgreSQL schema, constraint, apply/reapply and rollback equivalence are checked
separately by the opt-in integration suite.

## Bounded database reads

The pgx profile also captures synchronous public GET handlers that return every
field of one flat model, explicitly ordered by its primary key and limited to a
literal 1–1000 rows. The limit is preserved from the source, never invented.
Field projections must list all fields in declaration order. Examples for a
model with fields `id` and `name`:

```python
# DRF: retain the explicit public/JSON decorators described above.
from models import Widget


def widgets(request):
    return Response(list(Widget.objects.order_by("id").values("id", "name")[:100]))
```

```python
# Flask/FastAPI: plain SQLAlchemy 2, with explicit environment-based setup.
from models import Widget
from os import environ
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

engine = create_engine(environ["DATABASE_URL"])


@app.get("/widgets")
def widgets():
    with Session(engine) as session:
        return [
            dict(row)
            for row in session.execute(
                select(Widget.id, Widget.name).order_by(Widget.id).limit(100)
            ).mappings()
        ]
```

For Flask, wrap the returned list comprehension in `jsonify(...)`. Framework
imports/app construction still follow the literal endpoint profile. This is not
Flask-SQLAlchemy extension capture. Joins, unsupported filters, partial projections, dynamic
pagination, custom sessions, and async database handlers block generation.

## First write profile

Fiber, chi, mux, and Gin with pgx accept one bounded create/PATCH/DELETE recipe for a captured flat model. DRF uses
`objects.create(...)`, `filter(primary_key=...).first()`, explicit field assignment and
`save(update_fields=...)`. Flask and FastAPI use a synchronous SQLAlchemy `Session`, `add`,
`get`, `commit`, and `refresh`. Routes must expose POST on a literal collection path and PATCH
on one integer primary-key path. The source handler must explicitly reject unknown fields,
missing required create fields, nulls for non-null fields, wrong JSON scalar types, and integers
outside the captured PostgreSQL width. Any changed statement, validation bound, response shape,
status, side effect, async handler, custom hook, or unsupported field leaves a capture gap.
DELETE on the same integer path must explicitly load the row, return the captured 404 for a
missing row, delete and commit it, then return an empty 204 response.

Generated handlers parse JSON without float conversion, distinguish missing from null/false/zero/
empty string, enforce a 1 MiB request body limit, use parameterized SQL, and execute each write through `pgx.BeginFunc`. Create returns
the inserted row with 201. PATCH updates only present fields, permits `{}` as a read-back, returns
404 for a missing row, and returns the updated row with 200. Invalid input returns 400; other
database failures return a generic 500. DELETE returns 404 or an empty 204 and commits through the
same transaction primitive. Each router uses its native path-parameter API and passes
the same generated lifecycle contract.

Public `test`/`verify` fail closed for captured writes until the versioned shared HTTP scenario
adapter can compare ordered requests and database effects safely. The extension does not reuse the
GET-only replay or mutate an arbitrary prepared fixture. CI instead runs the bounded lifecycle for
all four routers against an isolated PostgreSQL schema for every supported Python source recipe.

When a captured route reads data, generated `NewApp(pool *pgxpool.Pool)` takes an
existing non-nil pool; its caller owns opening, configuring, and closing that
pool. Pure schema/literal-endpoint projects retain `NewApp()`. Handlers use the
request context, scan typed nullable fields, check iteration errors, close query
rows, and encode the complete result before writing a response. Empty results
are `[]`; database failures return a generic JSON 500 without driver details.
That error response is a target safety contract, not Python default-error parity.

Public `test` requires `SANKA_GO_TARGET_TEST_DATABASE_URL` for these handlers.
`verify` additionally requires `SANKA_GO_SOURCE_TEST_DATABASE_URL`. Supply dedicated
fixture databases with matching data and already-applied source/target schemas;
ordinary `DATABASE_URL` is never used as an implicit replay destination. Use a
PostgreSQL URL for DRF/Go and a `postgresql+psycopg://` SQLAlchemy URL for
Flask/FastAPI. Keep fixture credentials outside migration configuration and plans.
Use read-only fixture credentials: replay executes candidate code and is not a
sandbox. The replay runner does not create schemas, migrate, or seed these databases.

Both lifecycle commands require the captured successful status and JSON media
type. Verify also compares real Python and Go response bodies. Reports describe
only the supplied fixture observations, not unseen rows, transactional writes,
or full schema equivalence. CI owns isolated source/target schemas and checks
empty/populated results, nulls, Unicode, integer boundaries, order, source limits,
fixture mismatch detection, and safe database-error responses for all twelve
source/target combinations.

## Request-driven string filters

Bounded reads can include one exact equality predicate on a captured string or
text field, including nullable text fields. The request supplies a string;
matching database NULL values is not part of this profile. Source forms are:

- DRF: insert `.filter(name=request.query_params.get("q", "first"))` before
  `.order_by(...)`.
- Flask: import `request` from Flask and insert
  `.where(Widget.name == request.args.get("q", "first"))` before `.order_by(...)`.
- FastAPI: declare `q: str = "first"` in the synchronous handler signature and
  insert `.where(Widget.name == q)` before `.order_by(...)`.

The column name, query parameter name, and default are captured from the source;
these examples are not reserved names. DRF and Flask require an explicit string
default in `get`; FastAPI requires a plain `str` annotation and string default.
Extra parameters, integer/boolean conversion, optional/null query values, custom
query validators, additional predicates, and alternative comparison operators
remain blockers. Unknown behavior is never replaced with an unfiltered read.

Generated SQL binds the request value as a PostgreSQL parameter. User values are
never interpolated into SQL. The source row limit and primary-key ordering still
apply. Missing and empty query values remain distinct. Repeated parameters use
Flask's first value or DRF/FastAPI's last value. The generated parser also
preserves each source's percent-decoding and malformed UTF-8 behavior; it does
not inherit whichever query parser the selected Go router happens to use.

Public `test`/`verify` expand filtered routes into deterministic request cases:
missing/empty values, repeated keys, Unicode, spaces/plus signs, percent escapes,
semicolons, and SQL-looking text. Verification compares actual source and target
HTTP responses for those requests against the supplied fixtures. Populate fixture
values to exercise matching and non-matching rows; an empty fixture proves only
empty-result behavior. CI seeds both varchar and nullable-text predicates,
checks filter/order/limit behavior and unchanged row counts, and verifies that a
changed candidate fixture is detected. Native parser tests additionally compare
all single-byte encodings and malformed UTF-8 boundaries with real Python clients.

## Remaining backend work

1. Capture whole projects into a framework-neutral contract, with stable model,
   field, relationship, operation, validation and policy identities.
2. Extend the initial pgx/Goose profile to richer schemas, data migration and
   transactional CRUD. Qualify each supported combination against a real database before
   advertising it; reject incompatible combinations.
3. Preserve validation/errors, auth/permissions, middleware, richer filtering,
   pagination, transactions and configuration. Introduce services only for
   captured business orchestration and repositories only for persistence needs.
4. Extend source/Go replay through the public lifecycle to cover failure paths,
   database effects, rollback, generated deployment entrypoints and shutdown.
5. Package/install verification and CI must pass before catalog publication.

Do not infer support from a framework appearing in the target choices: supported
behavior is defined by the capture gaps and independently exercised contracts.

The extension accepts `target` as an alias for `target_framework` for CLI integration.
Both must agree when supplied together; invalid or conflicting values are rejected.
If neither is supplied, Fiber remains the default. Both spellings produce the same
normalized configuration and extension plan hash. CLI forwarding is a separate runtime
change; this alias alone does not enable `--to` forwarding in existing CLI versions.

`test` and `verify` reuse Go 1.26.5 on `PATH`. When it is missing or a different
version is installed, they automatically download the qualified compiler from
Go's official distribution, verify its pinned SHA-256 and size, and install it
atomically under `.sanka/go-toolchain`. Subsequent runs reuse that installation.
When the CLI runs without `HOME`, build and module caches also live there;
direct runs retain their existing Go caches. System installations and shell settings
are untouched. Automatic installation supports macOS, Linux and Windows on amd64
and arm64. A failed download stops verification with retry/manual installation
guidance. Offline runs need an existing qualified compiler and cached modules.
Scan, planning and code generation do not download or execute Go.

Run the fresh-install regression (downloads into a temporary fixture and verifies
reuse without a second download):

```bash
SANKA_GO_BOOTSTRAP_TESTS=1 uv run python -m pytest \
  packages/sanka-extension-python-to-golang/tests/test_python_to_golang.py \
  -k native_go_bootstrap
```

The [transaction qualification contract](../../docs/python-to-golang-transactions.md)
covers rollback, commit failure, cancellation, and connection reuse for the pgx primitive used
by the first create/PATCH/DELETE profile. PUT and broader serializer/schema validation remain
outside the qualified profile.
