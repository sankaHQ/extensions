# Python to Golang (experimental)

Extension ID: `sanka/python-to-golang`. Sources: DRF, FastAPI, Flask.
Targets: Fiber (default), chi, Gorilla mux, Gin.

The current implementation produces a Go `backend` package exposing `NewApp` for
literal public JSON GET endpoints, bounded PostgreSQL reads, and the first qualified
create/PUT/PATCH/DELETE profile. Every target includes a runnable `cmd/api`. It is **not a complete backend migration**,
available as an experimental prerelease, and not qualified for production cutover.
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
an entrypoint plus explicitly imported top-level Python modules (up to 32 files),
and the selected models module for pgx. Unknown imports, decorators, configuration,
request parameters, unsupported dynamic handlers, unreferenced Python modules, and
source symlinks block generation. DRF endpoints require explicit public permission, no authentication,
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
PATH. By default the extension interpreter needs the source framework and its test-client
dependencies installed. For a CLI-installed converter, install those dependencies
in a separate source virtual environment, set `SANKA_GO_SOURCE_PYTHON` to its
absolute `bin/python` path, and pass `--extension-env SANKA_GO_SOURCE_PYTHON`
to `sanka verify`. The interpreter path is explicit and must be executable; it is
not discovered from the working directory. Replay retains Python `-I` isolation,
so `PYTHONPATH` does not inject source dependencies. The report records the source
interpreter path and version. This leaves the locked converter wheel environment
unchanged. Go may download the checksum-pinned dependencies.

Replay executes source and candidate code; the temporary directory is not a
security sandbox. It does not start listening servers or alter candidate files.
For DRF, the qualified fixture settings use no installed apps and no unauthenticated
user model; external application settings and middleware are not captured.
Reports include source and candidate digests. Manual handler repairs are tested,
but changes to Go locks or the captured contract are rejected. A mismatch returns
an error with a report path. Each rerun invalidates its old report first, so a
failed rerun cannot leave stale passing evidence.

## Project routing

The same handler and database contracts work with Flask Blueprints, FastAPI
APIRouters, and DRF `path(prefix, include([...]))` URL lists. Flask registration
prefixes override the Blueprint prefix; FastAPI registration prefixes are added
to the APIRouter prefix. Static prefixes and one registration per router are
qualified. Routes must be defined before registration.

DRF multi-method `api_view` functions can dispatch using explicit
`if request.method == "PATCH"` / `elif` branches (or separate `if` branches),
with one qualified handler body for every declared method. Generation splits
those bodies into Go method routes while source tests retain Django URL dispatch.
Overlapping Django URL registrations are blocked: separate views at the same
path do not provide method-based dispatch in Django.

Explicit imports such as `from routes import api` or `from views import health`
are captured without importing source. Every consumed file participates in the
plan hash and is copied unchanged into the source replay environment. Cyclic or
repeated local imports, aliases, package-relative imports, conflicting names,
and missing module globals block generation.

A Flask factory may take no arguments, construct `app = Flask(__name__)`, register
blueprints, and return the app, followed by `app = create_app()` at module scope.
Factory configuration, hooks, nested router registration, custom dependencies,
General Pydantic models, DRF serializers/ViewSets and custom authentication still require
additional capture. This routing support does not imply whole-project parity.

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
`database_dialect: "postgresql"`, `migration_tool: "goose"`, and
`models_file: "models.py"`. `schema_mode` is `"empty"` by default or may be
`"adopt-existing"`. Other database/tool combinations are rejected.

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
migration in `empty` mode rejects existing application relations. Reapplying an
applied baseline is a no-op. `go run ./cmd/migrate down` drops tables created by
that baseline.

`adopt-existing` is a validation-only baseline for a database already created by
the captured Python models. It checks the table and ordered-column set, types,
nullability, identity/sequence ownership, primary and unique constraints, and
rejects extra application tables, unsupported constraints, user triggers and row
security. Extra indexes are preserved. It takes `ACCESS SHARE` locks on captured
tables during validation, so run it in a controlled schema-change window.
Application rows are never changed; `down` only unregisters the Goose baseline.
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

The same profile recognizes one exact integer-primary-key detail GET. DRF must
use `filter(id=id).first()`; Flask and FastAPI must use `Session.get`. Each handler
must return every field in declaration order and an explicit source-shaped 404.
Generated Fiber, chi, mux, and Gin handlers parse the route ID, run one
parameterized query, and return the captured object, 404, or a generic database
error. Public replay replaces the captured parameter with a deterministic fixture
ID and compares the real Python and Go responses against independently seeded
PostgreSQL schemas.
Malformed or negative path-parameter parity remains outside this bounded profile.

## First write profile

Fiber, chi, mux, and Gin with pgx accept one bounded create/PUT/PATCH/DELETE recipe for a captured flat model. DRF uses
`objects.create(...)`, `filter(primary_key=...).first()`, explicit field assignment and
`save(update_fields=...)`. Flask and FastAPI use a synchronous SQLAlchemy `Session`, `add`,
`get`, `commit`, and `refresh`. Routes must expose POST on a literal collection path and PUT/PATCH
on one integer primary-key path. The source handler must explicitly reject unknown fields,
missing required create fields, nulls for non-null fields, wrong JSON scalar types, and integers
outside the captured PostgreSQL width. Any changed statement, validation bound, response shape,
status, side effect, async handler, custom hook, or unsupported field leaves a capture gap.
DELETE on the same integer path must explicitly load the row, return the captured 404 for a
missing row, delete and commit it, then return an empty 204 response.
PUT on that path must validate the complete replacement, assign every writable field, save or
commit it, and return the replaced row with 200.

Generated handlers parse JSON without float conversion, distinguish missing from null/false/zero/
empty string, enforce a 1 MiB request body limit, use parameterized SQL, and execute each write through `pgx.BeginFunc`. Create returns
the inserted row with 201. PATCH updates only present fields, permits `{}` as a read-back, returns
404 for a missing row, and returns the updated row with 200. Invalid input returns 400; other
database failures return a generic 500. PUT replaces all writable fields and returns 404 or the
updated row with 200. DELETE returns 404 or an empty 204 and commits through the
same transaction primitive. Each router uses its native path-parameter API and passes
the same generated lifecycle contract.

Public `test`/`verify` use `sanka-http-replay` for captured writes. Supply dedicated,
resettable fixtures through `SANKA_GO_TARGET_TEST_DATABASE_URL` and, for `verify`,
`SANKA_GO_SOURCE_TEST_DATABASE_URL`. Both must identify PostgreSQL hosts and databases;
SQLAlchemy source URLs use `postgresql+psycopg://`. Source and target must resolve to
different database schemas. Never use a customer or production database: the runner
resets captured tables and identity sequences before executing requests. Final fixture
effects remain available for inspection. `schema_mode=adopt-existing` is rejected.

Requests run in order without TCP listeners. Responses, all captured table rows and
sequence state are compared using the versioned shared observation contract. Bigint
model values and sequence counters use decimal strings in observations. Source,
candidate and scenario digests identify the evidence; source/candidate/scenario drift
invalidates the run. Observation reports fail above 16 MiB rather than truncating
evidence. The source Python environment can be selected with
`SANKA_GO_SOURCE_PYTHON`, as for GET replay.

Place an ordered `sanka-verify.json` in the source root to provide explicit scenarios:

```json
{"schema":"sanka.http-scenarios/v1","scenarios":[
  {"id":"invalid-create","method":"POST","path":"/widgets","body":{},"expected_status":400},
  {"id":"create","method":"POST","path":"/widgets","body":{"name":"alpha","count":7,"enabled":true},"expected_status":201},
  {"id":"delete","method":"DELETE","path":"/widgets/1","expected_status":204}
]}
```

Without that file the shared generator supplies deterministic default scenarios.
Field-constrained endpoints require explicit scenarios because generic sample values
may violate their constraints. Default scenarios may expose unsupported native errors
(such as invalid path parameters); a failed comparison remains a failure, not a claim
of parity. Choose scenarios covering the source contract, including negative cases.
The acceptance matrix exercises all three Python sources and four Go routers.

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

## Strict Pydantic write schemas

Flask and FastAPI can use explicit Pydantic validation before the existing
SQLAlchemy POST/PUT/PATCH recipes. Capture recognizes flat `BaseModel` classes,
including classes imported from flat local modules, with exactly
`ConfigDict(strict=True, extra="forbid")`. Every writable database field must be
declared with `Field(...)`; integer fields need the captured int32/int64 `ge`/`le`
bounds. Nullable fields use `T | None` and `Field(default=None)`.

```python
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class WidgetInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str = Field()
    count: int = Field(ge=-2147483648, le=2147483647)
    enabled: bool = Field()
    note: str | None = Field(default=None)
```

Inside a qualified handler, replace its manual validation block with:

```python
try:
    data = WidgetInput.model_validate(data).model_dump(exclude_unset=True)
except ValidationError:
    raise HTTPException(status_code=400, detail="invalid request body")
```

Flask uses `return jsonify({"error": "invalid request body"}), 400` instead.
Its existing `data = request.get_json()` remains before validation. FastAPI keeps
`data: dict`; automatic request-model injection and its native 422 error format
are not captured by this recipe.

PATCH uses a separate schema with `default=None` on every field, retaining
nonnullable annotations for nonnullable columns. Pydantic does not validate these
omitted defaults; `exclude_unset=True` removes them. Explicit null is still rejected
for nonnullable fields. False, zero, empty string and explicit nullable null remain
present. No source code runs during capture; qualified schemas lower to the same
Go decoder as manual validation when no additional constraints are present.

String fields also accept literal nonnegative `min_length` and `max_length`.
Integer `ge` and `le` may narrow the database integer range. Contradictory bounds
block capture. These constraints belong to each endpoint, including PATCH, and
nullable fields retain missing-versus-null behavior. String lengths count Unicode
code points, not bytes. For example:

```python
name: str = Field(min_length=2, max_length=40)
count: int = Field(ge=0, le=100)
```

Coercion, custom validators/serializers, aliases, nested schemas, different defaults,
unsupported constraints, unused classes and inheritance beyond `BaseModel` block capture.
Tests compare Pydantic outcomes and values with native Go decoders for all four
routers, check invalid-object responses through original Python test clients, and
exercise generated CRUD on PostgreSQL in CI. Public ordered write replay is now
available; malformed/non-object body parity, native framework validation errors and
general DRF serializer behavior remain open.

## Strict DRF BaseSerializer validation

DRF can move its explicit strict flat-field validation into a `BaseSerializer`
subclass. Capture accepts only a `to_internal_value(self, data)` method that
branches on `self.partial`, performs the existing full/partial write validation,
raises `ValidationError("invalid request body")` on failure, and returns `data`
unchanged. Import `BaseSerializer` and `ValidationError` directly from
`rest_framework.serializers`. Imported schema modules are captured and hashed.
The [executable serializer fixture](tests/test_golang_drf_validation.py) defines
the complete supported method and handlers.

POST/PUT/PATCH handlers start with this sequence, using `partial=False` for
POST/PUT and `partial=True` for PATCH:

```python
serializer = WidgetInput(data=request.data, partial=False)
if not serializer.is_valid():
    return Response({"error": "invalid request body"}, status=400)
data = serializer.validated_data
```

The remaining qualified ORM recipe reads `data` instead of `request.data`.
Missing fields, null, false, zero and empty strings retain their existing meaning.
Source and Go tests compare accepted values and rejected payloads; native DRF
requests check POST/PUT/PATCH error bodies without opening a server. Generated
PostgreSQL CRUD fixtures cover grouped routes on all four targets in CI.

Field-based `Serializer`/`ModelSerializer`, coercion, custom persistence methods,
representation hooks, extra validation, `many=True` and native field-level error
responses remain unsupported. This contract does not imply arbitrary serializer
translation. Both Pydantic and DRF capture reject schema names shadowed by handler
locals, so normalization cannot hide an unbound or incorrectly resolved name.

## Write qualification

The CI qualification matrix executes original DRF/Flask/FastAPI requests through
their real routers and the generated Fiber/chi/mux/Gin handlers, for both manual
and strict schema validation. Each side has a separately created PostgreSQL schema.
The matrix compares status, JSON or empty body, content type, ordered rows and
sequence state after every request. It covers create, PATCH, replacement, deletion,
invalid fields, missing rows and subsequent ID allocation. A deliberate database
mutation must fail comparison even when the HTTP response matches. Successful
DELETE preserves the source framework's empty-body content type.

The public shared runner is also exercised through `verify` against independent
PostgreSQL schemas, including repeated baseline resets and a database-only mutation
that must fail comparison. Authentication, malformed-body/lookup parity,
custom database errors and general business operations remain outside this corpus.

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
by the first create/PUT/PATCH/DELETE profile. Broader serializer/schema validation remains
outside the qualified profile.

## Experimental public release

The scoped `api-converters-v0.1.0a1` GitHub prerelease contains the reviewed
wheel closure and SHA-256 manifests. Install with the published CLI 0.2.12:

```bash
RELEASE_COMMIT=$(git ls-remote https://github.com/sankaHQ/extensions.git refs/tags/api-converters-v0.1.0a1 | cut -f1)
test "${#RELEASE_COMMIT}" -eq 40
sanka extension marketplace add https://github.com/sankaHQ/extensions.git --revision "$RELEASE_COMMIT" --name api-converters --trust
sanka extension add sanka/python-to-golang --marketplace api-converters
```

This explicit marketplace pin does not change the CLI default catalog. The release
gate reproduces the small public literal-GET example through all five CLI stages
and compares actual source/target HTTP responses. See the
[examples](https://github.com/sankaHQ/sanka-examples) for source setup, reviewed-plan
checks, target toolchains and supported behavior. Database/write scenarios remain
outside that example qualification; broader package fixtures are tested separately.
