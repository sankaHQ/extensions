# Python to Go

`sanka/python-to-golang` converts captured DRF, Flask, and FastAPI APIs to Fiber
(the default), chi, Gorilla mux, or Gin. It generates a Go `backend` package and a
runnable `cmd/api`. Choose `database_layer: "none"` for literal JSON endpoints or
`"pgx"` for the supported PostgreSQL models, reads, and writes.

This is an experimental converter. It handles the source patterns documented below,
including conventional DRF projects, flat CRUD, and explicit auth and transaction
recipes. It does not promise to convert an arbitrary Python application, move
all existing data, or make a generated service ready for production cutover. A ready
scan means the captured code can be generated; it is not a cutover verdict.
The conventional PostgreSQL DRF profile includes an opt-in transfer of captured
tables between isolated schemas, but does not move the rest of an application.

The main contracts are [routing and package imports](#project-routing),
[database models and migration setup](#postgresql-schema-profile),
[reads](#database-reads), [writes](#flat-crud),
[authentication](#authentication-and-row-access), and
[conventional Django projects](#conventional-django-project-profile).

## Install the published prerelease

The published `api-converters-v0.1.0a6` GitHub prerelease contains the converter
and dependency wheels with SHA-256 manifests. With the published CLI 0.3.0, pin it
in a separate marketplace entry:

```bash
RELEASE_COMMIT=$(git ls-remote https://github.com/sankaHQ/extensions.git refs/tags/api-converters-v0.1.0a6 | cut -f1)
test "${#RELEASE_COMMIT}" -eq 40
sanka extension marketplace add https://github.com/sankaHQ/extensions.git \
  --revision "$RELEASE_COMMIT" --name api-converters --trust
sanka extension add sanka/python-to-golang --marketplace api-converters
```

This does not change the CLI's default catalog or install an unpublished candidate.
The public release check runs pinned examples through Scan, Plan, Apply, Test,
and Verify and compares source and target HTTP responses. See
[sanka-examples](https://github.com/sankaHQ/sanka-examples) for that example's
setup and plan review. The FastAPI row-transfer command requires the published
0.1.0a7 wheel. Alembic `add_column` lowering requires the 0.1.0a8 wheel after
that prerelease is published. Do not install a manifest that points
to an unpublished asset.

## Start with a project

Point `source_file` at the app entrypoint. For a database-backed project, also set
`models_file` to the model module. For example:

```json
{"source_framework":"drf","target_framework":"fiber","source_file":"shop_config/urls.py","models_file":"orders/models.py","database_layer":"pgx"}
```

For a small Flask app without database code, use:

```json
{"source_framework":"flask","target_framework":"fiber","source_file":"app.py","database_layer":"none"}
```

The CLI runs the extension through its JSON subprocess command,
`sanka-extension-python-to-golang`. Run Scan first and read its `gaps` and
`generation_ready` fields. Plan records the exact generated files and a stable
`plan_hash`. Review that plan before Apply. Apply requires the runtime's
`reviewed_plan_hash` and the matching `extension_plan_hash`; it recomputes the plan
and refuses changed source, changed configuration, or an existing output directory.
Generated files live under the project's `.sanka/.../golang` artifact directory.

With a candidate wheel that includes the conventional DRF profile, run:

```bash
CONFIG='{"source_framework":"drf","target_framework":"fiber","source_file":"shop_config/urls.py","models_file":"orders/models.py","database_layer":"pgx"}'
sanka scan . --extension-config "$CONFIG"
sanka plan . --to fiber --extension-config "$CONFIG"
```

Review the returned plan hash and files before `sanka apply --plan-hash <hash>`.
Then run `sanka test` and `sanka verify` with the fixture environment variables
described below. CLI 0.2.12 needs both `--to fiber` and the matching
`target_framework` configuration; `--to` alone does not reach the extension in
that version.

Scan, Plan, and Apply parse Python source without importing or executing it. They
follow statically resolvable local imports, with at most 32 executable modules and
10 MB in that module graph. The wider source inventory can contain up to 20,000
regular files and 256 MiB. Unknown executable code, dynamic handlers, unsupported
decorators or configuration, and source symlinks stop generation. The public
APIView profile requires explicit permissions, empty authentication, and a JSON
renderer; the signed-auth profiles below add their own explicit policies.

Test runs the generated Go candidate. Verify also runs the captured Python source
and compares the selected HTTP scenarios. Both execute code, so use trusted source
and disposable fixture databases. See [verification and fixtures](#write-qualification)
for database checks and [the conventional DRF project profile](#conventional-django-project-profile)
for `ModelViewSet` projects.

## Authentication and row access

### Explicit bearer middleware

The converter recognizes a bounded environment-backed access policy in all three
sources: FastAPI HTTP middleware, Flask `before_request` / `after_request`, or DRF
`decorator_from_middleware` around every captured view. The exact supported source
recipes are executable in [test_golang_security.py](tests/test_golang_security.py).
They compare UTF-8 bearer bytes with `hmac.compare_digest`, explicitly trim space/tab
around the Authorization value, and use separate `AUTH_READ_TOKEN` and
`AUTH_WRITE_TOKEN` environment credentials. Missing, equal or non-ASCII credentials
return 503; invalid or absent authorization returns 401 with `WWW-Authenticate:
Bearer`; the reader credential cannot perform writes (403). Both credentials allow
reads. Generated applications require operators to supply credentials; no credential
values are captured or embedded in application defaults.

Literal Cache-Control, X-Content-Type-Options, X-Frame-Options, Referrer-Policy and
Strict-Transport-Security response assignments preserve framework hook order,
including early denial behavior. No extra service/repository layers are added.
OAuth, sessions, user identity/ownership permissions, arbitrary `Depends` policies,
custom authentication code and unrecognized middleware still block generation.
This profile does not qualify implicit HEAD/OPTIONS behavior or unmatched-path errors.

Replay supplies synthetic credentials to isolated probe processes. For every existing
scenario it runs the authorized writer case, absent and invalid authorization, and
the reader case (403 on writes). Scenario files must omit Authorization; replay owns
that header. It compares selected response headers through an extension-owned
`sanka.go-security-headers/v1` sidecar without changing shared HTTP observation v1.
Write verification compares rows and sequences after every request and checks that
each denial leaves them unchanged. Source and candidate code execution still requires
trusted fixtures; replay is not a security sandbox or production authorization audit.

### Signed bearer tokens

The same middleware positions also accept the explicit HS256 PyJWT recipe in
[test_golang_jwt.py](tests/test_golang_jwt.py). It uses `AUTH_JWT_SECRET` (32–256
ASCII characters), `AUTH_JWT_ISSUER`, and `AUTH_JWT_AUDIENCE`; missing or invalid
configuration returns 503. Operators must supply a randomly generated signing
secret. Values are never embedded in plans or generated application defaults.
The source replay environment needs PyJWT 2.14; it is not an extension dependency.
Generated JWT applications alone receive checksum-pinned `golang-jwt/jwt/v5`.

The qualified token has exactly the `HS256` algorithm and `JWT` type headers, a
canonical unpadded base64url encoding, and required `exp`, `sub`, `tenant`, `role`,
`iss`, and scalar `aud` claims. Optional `iat` and `nbf` must not be in the future.
Dates are integer seconds, bounded to 0–9007199254740991, with no clock leeway;
expiry is exclusive. Subject, tenant, and role use 1–128 ASCII letters, digits,
underscores or hyphens. Additional headers/claims and different source policies
are outside this profile. Token values are bounded to 8192 characters.

Invalid tokens return 401 with a Bearer challenge. Signed `reader` tokens allow
reads; `writer` allows reads and writes. Other signed roles and reader writes
return 403. Claims are verified for this access decision only: this does **not**
add row ownership, tenant isolation, identity propagation, login/token issuance,
refresh/revocation, key rotation/JWKS, or OAuth. Sources using those behaviors remain
blocked. The native bindings below extend this same bounded policy.

Public replay uses synthetic signed tokens and checks wrong signatures, algorithm
confusion, expired/future dates, missing or malformed claims, issuer/audience
mismatch, and denied roles. The database qualification checks rows and sequences
after denied requests using independent fixture schemas. Runtime clock-boundary
behavior still depends on the clocks of the two applications.

### Native authentication and handler identity

The same signed-token contract can be expressed through FastAPI's `Depends`,
DRF's `BaseAuthentication`, or Flask's `before_request` with `g.principal`.
[test_golang_identity.py](tests/test_golang_identity.py) contains executable source
recipes. FastAPI routes inject `principal: dict = Depends(authenticate)`; DRF views
explicitly select the captured authenticator and may use `IsAuthenticated` or
`AllowAny`. The DRF authenticator returns a user with `is_authenticated=True` and
`pk` from the verified subject, plus the verified claims as `request.auth`.

Generated adapters put verified subject, tenant and role into the individual
request's Go context. GET handlers can return explicit JSON projections from
`principal["sub"|"tenant"|"role"]`, `request.auth[...]` / `request.user.pk`, or
`g.principal[...]`. No service or repository layer is added for these reads.
Replay alternates users and tenants, including denials and returning to the first
user, and compares actual source and Go identity responses. The native policy
also composes with the qualified database reads and CRUD recipes; isolated
PostgreSQL replay verifies that denied writes leave rows and sequences unchanged.

Native exceptions preserve the source error envelope: FastAPI/DRF use `detail`,
Flask uses `error`; 401 includes the Bearer challenge. Every captured FastAPI/DRF
route must select the same qualified authenticator. Mixed public/protected routes,
other dependency graphs, custom permission classes, additional response hooks,
and identity-dependent business expressions remain blockers. The explicit row predicates
below extend the qualified database profile; other query policies remain unsupported.
As with the other profiles, malformed-body, unmatched-route and implicit
HEAD/OPTIONS behavior are outside the qualification corpus.

### Tenant and owner row predicates

Native signed-token handlers can explicitly scope a nonnullable string column to
verified `tenant` or `sub`. Column names are taken from the source, not inferred.
The executable recipes in [test_golang_row_security.py](tests/test_golang_row_security.py)
cover DRF, Flask and FastAPI on all four Go targets.

For lists, DRF uses `.filter(tenant_id=request.auth["tenant"])` directly on the
model manager; SQLAlchemy uses `.where(Model.tenant_id == principal["tenant"])`
directly after `select(...)`. Flask uses `g.principal`; DRF also accepts
`request.user.pk` for the subject. Multiple predicates use keyword arguments or
SQLAlchemy `where` arguments, with AND semantics. Existing ordering, limits and
qualified filtering/pagination remain in place.

Detail reads, updates and deletes use the existing explicit lookup followed by
`if item is None or item.tenant_id != principal["tenant"]:` and the existing 404
response. Writes require this guard immediately after strict body
validation and before persistence:

```python
if "tenant_id" in data and data["tenant_id"] != principal["tenant"]:
    raise HTTPException(status_code=403, detail="permission denied")
```

Use the corresponding Flask/DRF 403 response recipe; DRF manual validation uses
`request.data`. PUT/PATCH body predicates must match every row predicate, so a
request cannot change ownership. Multiple body predicates are consecutive guards.
Unknown policy expressions, nullable/non-string ownership fields and coercing
serializer profiles block generation. Guards never create a policy absent from
the source, and unscoped source routes remain unscoped.

Generated reads, UPDATE and DELETE bind identity as SQL parameters. Empty PATCH
also performs a scoped lookup. The implementation reuses the existing direct
handlers and transactions. It does not add database row-level security, shared
resource policies, administrator bypasses or concurrent ownership-transfer semantics.

Scoped writes require explicit `sanka-verify.json` scenarios using the synthetic
`fixture-tenant` / `fixture-user` identities. Replay independently changes tenant
and subject, tries ownership-changing bodies, and sends PUT/PATCH bodies matching
the attacking principal against the original row. It expects 404 for inaccessible
rows and 403 for body ownership violations. Source and Go observations include
initial database snapshots, so every denial must leave rows and sequences unchanged.

Generated Go dependencies and checksums are pinned in this package; Go 1.26.5 is
the qualification toolchain. No Go dependencies are installed into Sanka's Python
environment. No services or repository interfaces are generated for these direct operations.

The literal-GET matrix compares successful JSON responses with real Python
framework clients for all three sources and four Go routers. That test does not
establish default-error, HEAD/OPTIONS, redirect, content-negotiation, or deployment
parity. The larger installed-wheel matrix covers the additional profiles below.

For literal GET routes, Test runs Go tests against a temporary copy of the
generated package. Verify also runs the captured Python module through its
framework test client and compares status, JSON body, and media type. Both require
the saved plan and generated output to be unchanged. Go 1.26.5 is required; the
[toolchain setup](#target-selection-and-go-toolchain) installs it locally when
needed. Go may download checksum-pinned modules.

Verify needs a Python interpreter with the source framework and test-client
dependencies. For a CLI-installed converter, put those dependencies in a separate
virtual environment, set `SANKA_GO_SOURCE_PYTHON` to its absolute `bin/python`,
and pass `--extension-env SANKA_GO_SOURCE_PYTHON` to `sanka verify`. The converter
does not search the project for an interpreter. Replay uses Python `-I`, so
`PYTHONPATH` cannot inject dependencies. The report records the interpreter path
and version; the locked converter wheel environment stays unchanged.

Replay executes source and candidate code; the temporary directory is not a
security sandbox. It does not start listening servers or alter candidate files.
The literal DRF APIView fixture uses no installed apps or unauthenticated user
model. The [conventional Django project profile](#conventional-django-project-profile)
has separate settings and model rules.
Reports include source and candidate digests. Manual handler repairs are tested,
but changes to Go locks or the captured contract are rejected. A mismatch returns
an error with a report path. Each rerun invalidates its old report first, so a
failed rerun cannot leave stale passing evidence.

## Project routing

The same handler and database contracts work with Flask Blueprints, FastAPI
APIRouters, and DRF `path(prefix, include([...]))` URL lists. Flask registration
prefixes override the Blueprint prefix; FastAPI registration prefixes are added
to the APIRouter prefix. Static prefixes and one registration per router are
qualified, including nested registrations. Each child must be complete before its
parent is mounted. Routes must be defined before registration.

DRF multi-method `api_view` functions can dispatch using explicit
`if request.method == "PATCH"` / `elif` branches (or separate `if` branches),
with one qualified handler body for every declared method. Generation splits
those bodies into Go method routes while source tests retain Django URL dispatch.
Overlapping Django URL registrations are blocked: separate views at the same
path do not provide method-based dispatch in Django.

### Local imports and source inventory

Explicit imports such as `from routes import api` or `from views import health`
are captured without importing source. Every consumed file participates in the
plan hash and is copied unchanged into the source replay environment. Explicit
package imports (`from service.api.routes import api`, `from .routes import api`)
and repeated imports of the same declared symbol qualify. Packages require regular
`__init__.py` files containing only docstrings or `pass`; initializer behavior,
cycles, aliases, conflicting names and missing module globals block generation.
The executable module graph is bounded to 32 files and 10 MB.

`source_file` and `models_file` accept canonical project-relative Python paths,
including nested entrypoints such as `app/main.py`. Scan and plan hash up to 20,000
regular files and 256 MiB of source deterministically. Python modules outside the
qualified semantic graph produce one bounded gap with representative paths; they
never disappear from the source digest or become generated placeholders.

### FastAPI topology and persistence

FastAPI plans also inventory a static application factory, lifespan expression,
middleware registration order, installed exception handlers, nested APIRouter graph,
route order, and application/router/include/decorator/parameter `Depends` and `Security`
lists in their source order. Local and package imports are resolved without importing
source. Dynamic router factories, prefixes, paths or dependency lists remain explicit
topology gaps; topology capture alone does not enable lowering.

FastAPI scans also inventory conventional persistence declarations throughout
the project. The static capture records Pydantic v2 field annotations, required versus
nullable state, literal defaults, aliases, constraints and model configuration;
SQLAlchemy 2 mapped columns, exact type expressions, foreign keys and relationships;
Alembic revision ancestry plus ordered upgrade/downgrade operations; and ordered
`AsyncSession` calls with explicit `begin` or `begin_nested` scopes. Files recognized by
this scanner no longer appear as generic unconsumed-module gaps.

For an empty target schema, a single linear chain of static Alembic `create_table`,
`add_column`, and non-unique `create_index` operations lowers to ordered Goose
migrations when its final columns, keys and indexes match the captured SQLAlchemy
models exactly. Added columns must follow the model order. Nullable columns have
no default; non-null columns require a static string, Boolean, or bounded integer
server default. Each downgrade must explicitly reverse its additions.
Reciprocal `relationship(back_populates=...)` declarations qualify only when a
single captured foreign key links the child and parent; Go operations continue
to use the foreign-key column. `migrate down` reverses the full chain.
Branches, other schema alterations, data migrations, ORM cascades or object traversal,
unqualified repositories, and richer domain/request/response models remain
explicit gaps. Migration lowering never runs against an existing schema.

FastAPI also lowers the same flat CRUD profile from async handlers using
`async with AsyncSession(engine) as session`. The engine must use
`create_async_engine(environ["DATABASE_URL"], poolclass=NullPool)`; `get`, `delete`,
`commit`, and `refresh` must be awaited, while `add` remains synchronous. The existing
qualified native Pydantic bodies and stable validation error handler apply unchanged.
Source verification uses the psycopg async driver through `postgresql+psycopg://`.

A handler may delegate its final session scope with `return await repository(...)`.
The repository must be a plain async function with unannotated positional arguments
passed unchanged by name and a single session scope. Explicit flat-module imports
are supported. The complete expanded body must match the qualified CRUD recipe;
missing awaits, extra side effects, changed commits and unknown operations block
generation. This emits the existing direct Go operations without extra layers.

Handlers may also receive `session: AsyncSession = Depends(get_session)`, where the
zero-argument async provider only yields a session from `AsyncSession(engine)`.
The handler can use the session directly, pass it unchanged as the first argument
of a plain async repository function, or construct `repository = WidgetRepository(session)`
and directly await one method. A repository class must have only an exact session-storing
constructor and qualified async methods; all methods must be consumed. Repository
arguments must keep their names and order.

Provider side effects, extra dependency options, inherited repositories, unconsumed methods,
nested transactions and arbitrary orchestration remain unsupported. Commits must occur
explicitly within the operation, never in dependency teardown.

Session dependencies also accept `session: Annotated[AsyncSession, Depends(get_session)]`
or a module-level alias of that exact annotation, imported from `typing`.
The session parameter remains the last positional parameter. Dependency aliases must
follow their provider declaration and precede their routes; extra metadata and
default values remain blocked. Explicit imports from flat local modules are supported.
An `async_sessionmaker(engine)` factory may replace `AsyncSession(engine)` scopes,
optionally with a literal `expire_on_commit=True` or `False`. Only calls without
arguments qualify; factory reconfiguration, custom session classes, bind overrides
and automatic-commit `factory.begin()` scopes remain blocked. Existing explicit
commit and refresh requirements still apply.

Async session and repository forms accept primary-key lookups and
primary-key-ordered lists using `await session.get(...)` or
`(await session.execute(select(...))).mappings()`. Lists retain the existing captured
projection, string equality filter and source limit. Streaming results, joins,
alternative ordering, multiple statements and side effects remain blockers.

FastAPI lists can also use the explicit limit/offset recipe in
[the executable read fixture](tests/test_golang_async_reads.py). It declares
`limit: str = "2", offset: str = "0"` (defaults may vary within the same bounds),
validates ASCII decimal input before executing the query, then calls
`.limit(int(limit)).offset(int(offset))`. Limits are 1–1000 with at most four
digits; offsets are 0–2147483647 with at most ten digits. Invalid input returns
400 with `{"detail":"invalid pagination"}`. Leading zeros are accepted within
those length bounds; repeated parameters use FastAPI's last value. The optional
captured string filter may precede these parameters. Pagination SQL uses bound
parameters, and primary-key ordering keeps pages deterministic for a fixed database
snapshot. Native `Query` constraints, cursor pagination and total-count envelopes
remain unsupported. Public read verification exercises valid and invalid pages;
ordered PostgreSQL replay also checks lookups, reads after deletion and unchanged
database rows and sequences after every GET.

Replay executes the original async Python source and awaits
engine cleanup; normalization is used only for static contract checking.

### Application factories and lifespan

Flask and FastAPI support a zero-argument `create_app` that constructs the app,
registers completed blueprints/routers, and returns it, followed by
`app = create_app()` at module scope. Literal uppercase string, integer and boolean
settings can supply qualified arguments. `DATABASE_URL = environ["DATABASE_URL"]`
may initialize the database engine. Values, imports and initializers are hashed;
configuration is never evaluated during capture. Computed settings, rebinding and
custom factory side effects remain blocked.

FastAPI accepts this explicit lifespan recipe:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    try:
        yield
    finally:
        await engine.dispose()
```

Pass `lifespan=lifespan` to `FastAPI`. It reuses the generated process's database
startup ping and pool cleanup. Source replay enters and exits the original
FastAPI TestClient context, including startup failure handling. Custom lifecycle
tasks, dependencies, authentication, and schema shapes outside the qualified
contracts still require additional capture. This support does not imply parity
for arbitrary projects. The packaged fixture in
[the project tests](tests/test_golang_project.py) combines settings, database and
model modules, a factory, nested routers, lifespan, CRUD, filtering and pagination;
PostgreSQL CI compares ordered HTTP responses, rows and sequences across all four
targets.

## Process entrypoint

Every target includes `cmd/api/main.go` and a configuration test. `PORT` defaults to 8080 and
must be an integer from 1 through 65535. Database-backed handlers also require
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
and server defaults), ORM object traversal and cascades, custom index behavior,
custom types, validators, managers,
and model methods remain blockers. Generated structs do not implement validation.

Single-column foreign keys may reference a captured integer primary key of the
same width. SQLAlchemy requires an explicit column type followed by
`ForeignKey("parents.id")`; `ondelete` may be `NO ACTION`, `RESTRICT`, `CASCADE`,
or `SET NULL` (nullable columns only). Literal `deferrable` and `initially` options
are preserved. Django accepts `models.ForeignKey(Parent, on_delete=models.DO_NOTHING)`
with optional `null` and `db_index`. Its `<field>_id` column inherits the parent's
integer width, index defaults to enabled, and the constraint is initially deferred.
Python-side Django deletion policies remain blocked. Tables are created in stable
dependency order and dropped in reverse order; cycles and unresolved references block
generation. No lazy relationship loading or ORM cascade behavior is invented.

Read handlers may project these scalar foreign-key columns. A bounded ordered list
may filter one such column using an integer path parameter of the same name:
`Child.objects.filter(parent_id=parent_id).order_by("id").values("id", "parent_id")[:2]`
or `select(Child.id, Child.parent_id).where(Child.parent_id == parent_id).order_by(Child.id).limit(2)`.
The existing full-field projection and explicit route requirements still apply.
Generated pgx queries bind both the parent key and limit. Replay checks multiple
parent keys, including an absent parent. Joins and object projections remain blocked.

Foreign-key schemas may use the existing scalar CRUD recipes when every write
handler explicitly catches `IntegrityError` outside its ORM scope and returns
409 with `{"error": "integrity conflict"}` (DRF/Flask) or
`{"detail": "integrity conflict"}` (FastAPI). Import the exception directly from
`django.db` or `sqlalchemy.exc`. For example, enclose the entire qualified
FastAPI write body in `try`, followed by:

```python
except IntegrityError:
    raise HTTPException(status_code=409, detail="integrity conflict")
```

Only this exact response contract is lowered. Broader exception catches, retries,
`finally`/`else` behavior, and catches inside the transaction remain blocked.
The generated handler classifies PostgreSQL integrity errors after pgx transaction
cleanup, including deferred failures at commit. Other database errors retain the
existing failure response. Database cascades remain database operations; no ORM
object relationship behavior is added.

Relational write verification requires explicit ordered `sanka-verify.json`
scenarios: create parents before children, exercise invalid references and deletion
effects, and include recovery after failures. Replay resets independent fixtures,
compares every captured table and sequence after every request, and independently
requires 409 responses to leave table rows unchanged. Sequence allocations may
advance on failed inserts. The integration corpus exercises parent/child CRUD,
unique and foreign-key failures, reassignment, restricted and cascading deletion,
and subsequent ID allocation across the source/target matrix.

Explicit business transactions use
Django `transaction.atomic()` or SQLAlchemy `Session(engine)` with `session.begin()`,
strictly validated nested input objects, and two or more ordered operations. Supported
operations are creates, primary-key lookups, full replacements, partial updates and
deletes. SQLAlchemy must explicitly flush each mutation. Primary and foreign-key
bindings may reference earlier live records. The response must snapshot the final
live record or a literal JSON object inside the scope and be returned after commit.
All generated statements use one pgx transaction.

Lookups must explicitly raise `LookupError("not found")` for missing records, caught
outside the scope with the source's 404 JSON response. This rolls back earlier creates,
updates and deletes. The existing outer integrity-conflict handler preserves 409s.
Partial updates require an explicit lookup key; omitted fields stay unchanged, null
is accepted only for nullable fields, and an empty update performs no UPDATE.

Generated key bindings are excluded from client input. A lookup whose key comes
from an earlier operation accepts an empty input object. Tests use explicit ordered
replay scenarios and combine authorization, permission checks, response middleware, related schemas,
validation, successful writes, whole-transaction rollback, and recovery across all
three Python sources and four Go targets. PostgreSQL cases require the documented
isolated fixture and run in CI; local compilation alone does not prove parity.

Nested transactions, conditional workflows beyond the missing-record guard and
partial-field assignments, arbitrary service calls, and external effects still
block capture. The async, service-delegation, and row-policy forms below extend
this contract.
SQLAlchemy lookups of a previously loaded model after an intervening deletion also
block capture until its identity-map behavior is modeled, including cascading and
SET NULL deletion effects. A model or module binding cannot shadow `LookupError`. No service/repository scaffolding is added for inline source orchestration.

After reviewing the generated SQL, set `DATABASE_URL` and run
`go run ./cmd/migrate up` from the generated directory. Migrations are embedded,
transactional and protected by a PostgreSQL session advisory lock. The initial
migration in `empty` mode rejects existing application relations. Reapplying an
applied baseline is a no-op. `go run ./cmd/migrate down` drops tables created by
that baseline.

`adopt-existing` is a validation-only baseline for a database already created by
the captured Python models. It checks the table and ordered-column set, types,
nullability, identity/sequence ownership, primary, unique and foreign-key constraints, and
rejects extra application tables, unsupported constraints, user triggers and row
security. Extra indexes are preserved. It takes `ACCESS SHARE` locks on captured
tables during validation, so run it in a controlled schema-change window.
Application rows are never changed; `down` only unregisters the Goose baseline.
Do not edit an applied baseline; later schema changes require new revisions.
Foreign-key adoption checks referenced tables/columns, update/delete actions,
match mode, deferral, and validation state; a mismatching constraint blocks adoption.

PostgreSQL schema, constraint, apply/reapply and rollback equivalence are checked
separately by the opt-in integration suite.

## Database reads

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

Generated `NewApp(pool *pgxpool.Pool)` takes a non-nil pool for database-backed
routes. Its caller opens, configures, and closes the pool. Literal-only projects
retain `NewApp()`. Read handlers use the request context, scan nullable fields,
check iteration errors, close rows, and encode the complete result before writing
a response. Empty results are `[]`; database failures return a generic JSON 500
without driver details. That error is a target safety contract, not Python
default-error parity.

For read-only replay, Test needs `SANKA_GO_TARGET_TEST_DATABASE_URL`; Verify also
needs `SANKA_GO_SOURCE_TEST_DATABASE_URL`. Use separate fixture databases with
matching seed data and already-applied schemas. Ordinary `DATABASE_URL` is never
used as an implicit replay destination. DRF/Go use PostgreSQL URLs; SQLAlchemy
sources use `postgresql+psycopg://`. Keep fixture credentials out of migration
configuration and plans. Read-only credentials are sufficient for this read
profile. Replay executes source and candidate code, so it is not a sandbox. It
does not create, migrate, or seed these read-only fixtures.

Both commands require the captured successful status and JSON media type. Verify
also compares Python and Go response bodies. Reports cover the supplied fixture
rows, not unseen data or full schema equivalence. CI checks empty and populated
results, nulls, Unicode, integer bounds, order, source limits, fixture mismatch,
and safe database errors across all three Python sources and four Go routers.

## Flat CRUD

The flat pgx profile accepts create, PUT, PATCH, and DELETE for a captured model
on all four Go routers. POST uses a literal collection path; the other methods
use one integer primary-key path. DRF source handlers use `objects.create(...)`,
`filter(primary_key=...).first()`, field assignments, and
`save(update_fields=...)`. Flask and FastAPI use synchronous SQLAlchemy `Session`,
`add`, `get`, `commit`, and `refresh` calls. The later async profile has its own
source recipe.

Source handlers must explicitly reject unknown fields, missing required create
fields, null for nonnullable fields, wrong JSON scalar types, and integers beyond
the captured PostgreSQL width. PUT validates a full replacement and assigns every
writable field. DELETE loads the row, returns the source's 404 if absent, commits
the delete, then returns an empty 204. Changed statements, validation bounds,
response shapes, statuses, side effects, custom hooks, and uncaptured fields stop
generation.

Generated handlers retain missing, null, false, zero, and empty string as distinct
inputs. They parse JSON without float conversion, cap bodies at 1 MiB, bind SQL
parameters, and use `pgx.BeginFunc` for each write. Create returns the inserted
row with 201. PATCH changes only present fields; `{}` reads the unchanged row.
PUT and PATCH return the row with 200 or a 404 for a missing ID. DELETE returns an
empty 204 or a 404. Invalid input returns 400, and other database failures return
a generic 500. Each router uses its own path-parameter API for this same contract.

Test and Verify use `sanka-http-replay` for captured writes. Set
`SANKA_GO_TARGET_TEST_DATABASE_URL` for Test and also
`SANKA_GO_SOURCE_TEST_DATABASE_URL` for Verify. Both must identify PostgreSQL
hosts and databases; SQLAlchemy source URLs use `postgresql+psycopg://`. Source
and target must resolve to different schemas. Use disposable databases: the runner
resets captured tables and identity sequences before requests. It leaves final
fixture effects available for inspection. It rejects `schema_mode=adopt-existing`.

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

Without the file, the shared generator supplies deterministic default scenarios.
Field-constrained endpoints need explicit cases because generic values may violate
their constraints. Defaults may also expose unsupported native errors, such as
invalid path parameters. A failed comparison remains a failure. Include negative
cases that matter to the source contract. CI exercises all three Python sources
and four Go routers.

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
changed candidate fixture is detected. Native parser tests compare
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
exercise generated CRUD on PostgreSQL in CI. Public ordered write replay covers
these routes; malformed/non-object body parity, native framework validation errors and
general DRF serializer behavior remain open.

FastAPI also accepts a bounded conventional request-model form. The handler injects
the matching flat `BaseModel`, immediately calls `model_dump(exclude_unset=True)`,
and uses the existing qualified SQLAlchemy write. Integer fields declare the exact
database-width `Field(ge=..., le=...)` bounds. Pydantic's ordinary integer and boolean
coercion and default extra-field ignoring are preserved in the Go decoder.

This form requires an explicit `RequestValidationError` handler registered through
`FastAPI(exception_handlers=...)` that returns status 422 with
`{"detail": "invalid request body"}`. This keeps the error contract stable across
source and generated applications. Default detailed FastAPI error arrays, custom
validators, aliases, nested bodies and other exception handlers remain blockers.

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

Custom persistence methods, representation hooks, extra validation, `many=True` and
native field-level error responses remain unsupported. This contract does not imply
arbitrary serializer translation. Both Pydantic and DRF capture reject schema names
shadowed by handler locals, so normalization cannot hide an unbound or incorrectly
resolved name.

## Conventional flat DRF Serializer fields

The same handler sequence can use one flat `serializers.Serializer` that exactly
matches a captured model's writable fields. The bounded field set is `CharField`,
`IntegerField` and `BooleanField`. Integer fields require the database-width bounds;
nullable fields require `required=False, allow_null=True`; strings preserve blanks
with `allow_blank=True, trim_whitespace=False`, and varchar fields require their
captured `max_length`.

Generated decoders preserve DRF scalar coercion, ignored unknown fields, nullable
values, full-write required fields and PATCH omission. Capture rejects changed field
options, custom hooks, aliases, extra fields, `ModelSerializer` and custom inheritance.
Source and Go decoder tests cover all four routers. Ordered PostgreSQL replay compares
the original DRF application and generated target when explicit fixture databases are
available.

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

## Target selection and Go toolchain

Selecting a framework does not imply that every source behavior is supported.
Scan reports the actual capture gaps; generation stops on any unclassified
executable behavior. Existing-data transfer, custom source policies and production
cutover need separate work and evidence.

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
covers rollback, commit failure, cancellation, and connection reuse for the pgx
transaction primitive. Serializer and schema support is limited to the source
forms documented above.

## Composed backend contracts

The representative backend fixture in `tests/test_golang_mixed_transactions.py`
combines CRUD, filtered reads, authentication, write permissions, response middleware
and four multi-record workflows. Its ordered scenarios cover successful relocation,
missing-record rollback after writes, unique/foreign-key conflicts, partial null/absent
updates, rollback of deletion, empty updates and recovery. Every source/target pair
compares HTTP responses, headers, all captured tables and sequences on isolated
PostgreSQL fixtures. A candidate that lets a write escape the transaction must fail
verification. These fixture results do not qualify arbitrary application workflows
or concurrent writers, triggers and model hooks outside the captured profile.

### Business workflows through services

Explicit transaction handlers may delegate through plain local service and repository
functions, including imported modules. Each wrapper must return the next function
with the same complete positional parameter list; async callers must await async helpers.
The terminal function owns the complete qualified validation, transaction, exception
mapping and response snapshot. Calls must be acyclic and at most eight functions deep.
Defaults, rebinding, arbitrary per-operation helper calls, extra side effects and
custom workflow classes remain unsupported. Static expansion preserves the source
module hashes and deterministic generated architecture; replay executes the original
modules. No additional service or repository interfaces are generated for delegation alone.

FastAPI supports the same mixed operations inside an explicit
`async with AsyncSession(engine)` and `async with session.begin()` pair.
`get`, `delete` and `flush` must be awaited; `add` is synchronous. The existing
pinned async engine/factory contract applies. Missing-row exceptions escape the
transaction before the outer 404 response, and response snapshots precede commit.
Nested transactions, concurrent writers, implicit commits and external effects
remain outside this profile.

Native signed-token transactions can scope every step to verified tenant or subject
claims through explicit missing-or-inaccessible guards. Create/update input guards
must precede the transaction, and update body policies must match row policies.
Generated lookups and mutations bind identity as SQL parameters; ownership-changing
inputs return 403 before persistence. Foreign keys in scoped writes must reference
an earlier scoped record rather than accept an unchecked relationship ID.

The service-workflow fixture exercises a multi-module project through all five
extension lifecycle commands, including async FastAPI, across all four Go targets.
A separate PostgreSQL matrix checks tenant denial, mixed-write rollback, partial
null/absent behavior and recovery. Cross-identity probes run before successful
mutations. Additional row-denial probes require the changed claim to guard an
input-key lookup of an existing record; lookups of newly created records do not
imply denial. Rolled-back creates may advance sequences; verification compares those
values between source and target and requires unchanged rows after 404/409.
These are bounded fixture contracts, not general business-logic or cutover qualification.


### Rich PostgreSQL values

The explicit validation profile captures UUID primary/foreign keys, dates, timezone-aware
timestamps, exact decimals and JSON values from DRF, Flask and FastAPI. Generated Go
uses the already-pinned pgx codecs. Rich values require explicit source validation
and serialization: UUIDs are canonical lowercase strings, dates are ISO calendar
dates, timestamps are UTC with six fractional digits, and decimals are strings with
exactly the declared scale, using explicit `format(value, "f")` responses. Inputs that
need rounding, timezone inference or numeric
coercion are blocked. Native serializer coercion for these fields is not yet qualified.

Django JSONField maps to JSONB; SQLAlchemy JSON and JSONB retain their storage type
and explicit `none_as_null` behavior. The bounded JSON write validator accepts
objects, arrays, strings, booleans, null and integers within the exact interoperable
range (±9007199254740991). Floating-point JSON inputs require a separate contract.
Database replay records the SQL-null flag alongside the JSON value, so JSON null
cannot silently stand in for SQL NULL.

Static string, boolean and integer Python defaults remain client defaults, not SQL
DEFAULT expressions. Full writes apply explicitly captured fallbacks; PATCH leaves
absent fields unchanged. Nullable SQLAlchemy defaults, callable/server defaults, custom encoders, naive timestamps,
expression/partial indexes and constraint options outside the captured profile block
generation. Named column indexes and composite uniqueness are preserved, and schema
adoption checks decimal precision/scale, timestamp precision and named index shape.
Django updates keep explicit primary keys immutable.

UUID path parameters use an explicit string-path validation guard and error response
in the source; implicit framework UUID conversion is not substituted. Reads require
explicit wire projections. Rich-value and relational replay requires ordered
`sanka-verify.json` scenarios. The rich-values fixture includes all three sources and
four routers for CRUD, plus shared pgx checks for JSON-null storage and UUID foreign-key
transaction rollback. PostgreSQL cases require the documented isolated test DSN;
a native codec/compile check alone does not establish database parity.


### Composed query and dependency contracts

List reads accept two to sixteen string equality predicates in one Django
`filter(...)` or SQLAlchemy `where(...)` call. All predicates are joined with AND,
remain in source order, and use bound SQL parameters. Unknown operators, types,
unused FastAPI parameters and duplicate Django keyword arguments block generation.
The existing single-predicate spelling remains supported.

DRF and Flask also support the bounded ASCII limit/offset contract already supported
for FastAPI. They read `limit` and `offset` through `request.query_params.get` and
`request.args.get`, respectively, with explicit string defaults and the same bounds:
limit 1–1000 (at most four digits), offset 0–2147483647 (at most ten digits).
Django uses `[int(offset):int(offset) + int(limit)]`; SQLAlchemy uses
`.limit(int(limit)).offset(int(offset))`. The full validation guard and 400 response
must be present in the source. The generated error retains `error` for DRF/Flask
and `detail` for FastAPI. Repeated query values retain each source's parsing rules.
[Executable examples](tests/test_golang_query_composition.py) show the complete forms.

Relationship lists can bind UUID foreign-key paths using the same explicit UUID
guard and wire projection as rich primary-key reads. Implicit UUID converters,
relationship paging and joins remain outside this contract.

FastAPI async session dependencies may be keyword-only, including
`Annotated[AsyncSession, Depends(get_session)]`, alongside defaulted filter and
pagination parameters. Direct handlers and the existing function/class repository
forms reuse the same scoped session lowering. Additional keyword-only parameters,
dependency metadata, default values on Annotated dependencies, and dependency
teardown commits remain blockers.

Native FastAPI Pydantic bodies preserve narrower integer `ge`/`le` bounds and
string `min_length`/`max_length`, including nullable and partial fields. Constraint
checks follow the existing native scalar coercion and retain the captured 422
response. Rich native field coercion remains unqualified; rich values still require
the explicit wire contract above.

The composition fixture combines native schemas, CRUD, conjunction filters,
pagination, and async injected repositories through public ordered replay. Native
checks compare actual Python and Go error responses without listening servers.
PostgreSQL checks require isolated source/target fixture databases and compare HTTP,
rows and sequences; skipped database tests do not establish database parity.

### Native UUID and decimal request fields

Qualified DRF `Serializer` and FastAPI Pydantic CRUD handlers can use native UUID
fields and decimals with explicit precision and scale matching the database.
The capture preserves typed validated values, nullable fields, and PATCH presence;
FastAPI dependency-injected async sessions and qualified repositories use the same
contract. UUID formats follow each source validator, including DRF's integer
inputs. Decimal validation and database coercion use exact decimal digits, with
Pydantic's default decimal-context validation and DRF's signed-zero response
behavior preserved. Pydantic decimals retain their original coefficient and exponent
until PostgreSQL applies the column scale and range rules. ORM defaults do not make a required serializer field optional.

The native profile requires the existing explicit response projection and stable
validation error handler. Native DRF handlers must read `validated_data` after validation;
mixing raw request values with coerced fields remains a capture gap. Custom validators,
decimal contexts, aliases, rounding
policies and custom native field hooks remain capture gaps. Existing
explicit wire-validation profiles remain supported. The generated native helpers
use Go's standard library and are emitted only for native rich-field contracts.

Qualification includes differential serializer/Go decoder tests and PostgreSQL
replay fixtures for DRF, synchronous FastAPI and async FastAPI on Fiber, chi, mux,
and Gin, including HTTP errors, exact numeric values, null/absent updates, deletes,
and table/sequence effects. Database tests require the documented fixture DSN.


### Packaged source projects and generation readiness

`source_file` and `models_file` stay relative to the project root. Regular Python
packages can live directly in the project or under a directory such as `src/`;
imports resolve from the outermost regular package, as they do in isolated replay.
Package initializers must remain declarative. Namespace-package execution,
import cycles, shadowed framework modules and dynamic imports remain capture gaps.

Scan includes `generation_ready` and `source_inventory.module_roles`, separating
application modules, the configured model module, source tests and unclassified
Python files. Unimported `tests/` files, `conftest.py`, `test_*.py` and `*_test.py`
are inventoried as source tests. Imported helpers remain application code even
when their filenames look like tests. Migration directories are never excluded by
these test conventions. Every source test remains in the source fingerprint, so
changing it invalidates the reviewed plan. Source assertions are not translated;
the conventional DRF opt-in below can execute selected tests during Verify.
Generated contract tests and source/target replay provide the default
qualification. Unclassified runtime behavior still blocks generation.

A ready scan means the captured project can be generated, not that it is ready
for production cutover. Test/verify reports include `qualification` flags for
candidate execution, source comparison, original-test execution and cutover
qualification. `test` executes the Go candidate; `verify` also compares the
captured Python source. PostgreSQL write verification compares rows
and sequences in independent resettable fixture schemas. Neither command marks
arbitrary application behavior or production deployment as qualified.


### Native dates, timestamps and JSON

The native DRF serializer profile accepts `DateField(input_formats=['iso-8601'])`,
`DateTimeField(input_formats=['iso-8601'], default_timezone=timezone.utc)`, and `JSONField`.
The explicit UTC policy avoids guessing the application's active Django timezone.
The FastAPI profile accepts `date`, Pydantic `AwareDatetime`, and `JsonValue` on flat
request models. Nullable fields and partial schemas retain the existing presence contract.
These declarations use the same stable error handlers and explicit response projections
as the other qualified native fields; custom formats, timezone policies, validators,
serializers and typed nested request models remain capture gaps.

Generated parsers retain source-specific calendar/week-date acceptance, numeric timestamp
coercion, microsecond truncation/rounding and timezone offsets. Dates remain calendar
values; persisted timestamp responses use the captured UTC microsecond projection.
Native JSON supports finite fractional numbers and arbitrary-precision integers in
nested objects/arrays. SQLAlchemy `none_as_null` and Django SQL NULL retain their
captured storage semantics, independently of JSON null. This does not widen the existing
explicit strict JSON validator's integer-only contract.

The release qualification job installs the candidate wheels through the pinned public
CLI marketplace, then runs packaged DRF, Flask, synchronous FastAPI and injected async
FastAPI projects against Fiber, chi, mux and Gin. It checks all five CLI commands,
repeated-plan hashes, rejected unreviewed apply, generated-file hashes, source preservation,
and ordered HTTP/database observations for invalid input, create, PATCH, replacement,
delete and recreation. The retained `go-project-acceptance.json` records each source/target
pair and its evidence.

The CLI fixture supplies both `--to` and `target_framework`; CLI 0.2.12 does not
forward `--to` to the extension by itself.
For malformed-Unicode scenarios, the pinned CLI 0.2.12 requires
`PYTHONIOENCODING=utf-8:backslashreplace` to print its JSON report without a surrogate
encoding error. The qualification runner sets this; JSON values round-trip unchanged.
The marketplace listener and disposable PostgreSQL schemas run only in the explicitly
enabled qualification job. No original source tests or production application are executed.

The same installed-wheel matrix includes composed DRF, Flask and sync/async FastAPI
projects with signed identity, read/write permissions, response-header hooks, related
models, filtered/paginated reads and tenant-guarded transaction services in separate
repository/service modules. Source policy is preserved per operation; this does not
invent tenant restrictions for source routes that do not declare them. Denied writes,
rollback, null/absent updates and response headers are compared against the source.
Native authentication can compose with literal response-header hooks: Flask
`after_request`, FastAPI response-only HTTP middleware, and DRF response middleware
decorating every captured view. Hook order is retained; arbitrary side effects block.

Process qualification starts the compiled generated application on each
router, checks invalid configuration and unavailable-database failures without exposing
credentials, then verifies SIGTERM drains an in-flight database request and closes
the pool. Transaction cancellation is checked separately with explicit cancelled and
expired contexts; client-disconnect cancellation is not implied by graceful shutdown.

Boundary probes separately verify that accepted extreme timestamps and PostgreSQL-invalid
JSON preserve lookup ordering and committed rows/sequences. These probes qualify storage
behavior, not parity of framework-specific unhandled-500 response bodies.


## Conventional Django project profile

A bounded DRF 3.18 project can use `manage.py`, static settings, multiple installed
apps, `DefaultRouter`, `ModelViewSet` or `ReadOnlyModelViewSet`, and
`ModelSerializer`. Configure the actual URL and model modules:

```json
{"source_framework":"drf","target_framework":"fiber","source_file":"shop_config/urls.py","models_file":"orders/models.py","database_layer":"pgx"}
```

This profile accepts a SQLite or PostgreSQL source and an empty PostgreSQL destination, using
pgx and Goose on all four routers. PostgreSQL sources may also validate an existing
schema with `schema_mode: "adopt-existing"`; the adoption migration changes no
application table. It captures string choices/defaults, integer,
boolean and decimal fields, unique fields, cascade foreign keys, and one explicit
nested serializer. Nested writes require the captured atomic parent/children
create and aggregate-limit rollback recipe; updates preserve the captured nested
ignore behavior. Unknown settings, hooks, queryset behavior and schema drift
remain blockers. Each app needs an initial schema migration; a linear sequence of
static `AddField` and `AlterField` revisions is folded into the final model schema.
Data operations, branching histories and unknown schema operations block generation.

Explicit cross-app dependencies and CASCADE foreign keys are checked against
the model declarations. Module-qualified
model and serializer identities distinguish equal class names across apps. Literal
nested URL includes retain declaration order; duplicate routes and import/include
cycles block generation. The standard `main()` startup wrapper and explicit import
aliases are supported without executing source code during scan or plan.

### ViewSet queries

`ReadOnlyModelViewSet` generates list and detail reads only. POST, PUT, PATCH,
and DELETE return 405 without touching the database; a project may use writable
and read-only ViewSets together. The exact collection-level `summary` action that
returns `{"count": self.get_queryset().count()}` is qualified. Other custom
actions and method overrides block generation.

The static parser also accepts the standard annotated `main() -> None` wrapper,
literal tuple model ordering and serializer fields, typed migration declarations,
generated primary-key metadata, and docstring-only package initializers. A
gadget-inventory project replay covers these forms on all four Go routers;
widget-inventory also passes static capture.

ViewSets also accept literal, exact scalar `queryset.filter(...)` predicates,
stock `OrderingFilter` with explicit scalar `ordering_fields`, and stock
`LimitOffsetPagination` with an optional static `PAGE_SIZE` (1–1000). Filters apply
to detail lookups before updates/deletes as well as lists. Pagination uses SQL
COUNT/LIMIT/OFFSET and preserves DRF response links, repeated query parameters and
invalid-parameter defaults. It adds no service layer. Custom filter/pagination
classes, relationship traversal, search/regex backends and random ordering remain
blockers. Database-overflow pagination errors remain outside JSON error-body parity.

The executable contract is in `tests/test_golang_drf_project.py`. A scalar
`validate_<field>` method may reject one literal string with a literal error.
Executable annotations, writable identity overrides, other custom validators,
nested uniqueness validators, and optional aggregate fields are blocked rather
than silently changed.

### Signed project authentication

The conventional project profile accepts the same statically matched DRF
`JWTAuthentication(BaseAuthentication)` recipe used by the native identity profile
when it lives in one installed app's `auth.py`. Each ViewSet must explicitly declare
either `[JWTAuthentication]` with `[IsAuthenticated]`, or empty authentication and
permission lists for a public route. The verifier reads `AUTH_JWT_SECRET`,
`AUTH_JWT_ISSUER`, and `AUTH_JWT_AUDIENCE` at runtime; captured contracts and
generated defaults contain no credential values. An environment-backed Django
`SECRET_KEY` must use a different variable. Verified reader tokens can read;
writer tokens can read and write. Missing or invalid tokens return 401 with the
Bearer challenge, reader writes return 403, and invalid runtime credentials fail
closed with 503. Authentication precedes a protected ViewSet's method denial.
Duplicate Authorization headers are rejected.

Isolated source-to-Go replay supplies synthetic tokens, exercises malformed claims
and denied writes, and compares responses, rows, and identity sequences after each
request across Fiber, chi, mux, and Gin. A protected ViewSet may bind read-only
string owner/tenant fields to `request.user.pk` and `request.auth['tenant']` in
`get_queryset` and `perform_create`; generated reads and writes bind those claims
in SQL. Replay checks cross-identity reads and writes. Session authentication,
custom permission classes, other JWT policies, and other identity-dependent
querysets remain blockers. Anonymous projects retain their prior behavior.

For SQLite sources, generated writes use transactional identity counters because SQLite rolls back
AUTOINCREMENT allocation with failed writes. IDs are allocated by the generated
handlers; direct SQL writers must not invent their own allocation. PostgreSQL sources
retain native sequence allocation, including values consumed by rolled-back writes.
Application startup, database migrations and shutdown reuse the existing runtime. No service
or repository layer is added for this CRUD profile.

### Source comparison

`verify` copies the classified source into an isolated process, checks DRF 3.18,
migrates a disposable SQLite database or newly created PostgreSQL source schema,
and compares JSON responses, captured rows
and logical identity state with an explicitly resettable PostgreSQL fixture.
Hosted-style `sanka-verify.json` cases with `setup` retain independent resets;
setup requests are observed too. Ordinary shared scenarios run in order. Original
source tests are fingerprinted, but are not translated or run by default.
For a trusted conventional DRF project with `app/tests.py` or
`app/tests/test_*.py` modules, set `SANKA_GO_RUN_ORIGINAL_TESTS=1` and forward it with
`sanka verify --extension-env SANKA_GO_RUN_ORIGINAL_TESTS`. Verify runs those
modules in a disposable copy after HTTP replay. The report records the module
names, test count and result; a failing or undiscovered test fails Verify.
SQLite DRF source tests use a disposable SQLite database. PostgreSQL DRF
source tests use a uniquely named test database on the supplied fixture server;
the fixture user must be able to create and remove databases. Verify removes
that database even when a source assertion fails and fails if removal fails.
For Flask and FastAPI projects, the same opt-in runs captured `test_*.py` files
with pytest in the disposable source copy. With a PostgreSQL database layer,
Verify creates a separate, empty database on the explicitly supplied source
fixture server, passes its URL as `DATABASE_URL`, and removes it after the tests.
The fixture user needs `CREATE DATABASE` and `DROP DATABASE` privileges. Source
fixture schema options are not carried into the new database; tests must set up
their own schema and data. A failed test or failed cleanup fails Verify. This
is opt-in execution of trusted source code, not a security sandbox or cutover
qualification.
Forward the fixture DSN and source interpreter through the CLI using
`--extension-env SANKA_GO_TARGET_TEST_DATABASE_URL` and
`--extension-env SANKA_GO_SOURCE_PYTHON` for test/verify.

For a PostgreSQL source, the captured Django database configuration must declare
`ENGINE = django.db.backends.postgresql` and `NAME`, `USER`, `PASSWORD`, `HOST`,
and `PORT` as distinct `os.environ["VARIABLE_NAME"]` lookups inside `DATABASES`.
Only variable names enter the contract. Verify also requires
`--extension-env SANKA_GO_SOURCE_TEST_DATABASE_URL`, pointing at an explicitly
owned PostgreSQL fixture where the source probe can create and remove its own
schema. Connection values come from that fixture URL, never the application's
database environment. This does not touch an application database during verification.

### Existing PostgreSQL rows

For a captured PostgreSQL DRF or FastAPI source, the generated
`tools/transfer_existing.py` copies only the contract's application tables to a
separately migrated, empty PostgreSQL target schema. FastAPI requires the
qualified linear Alembic history and every generated Goose revision applied.
The tool requires `psycopg` 3 in the invoking Python environment. The default
invocation is a read-only dry run:

```bash
export SANKA_GO_SOURCE_DATABASE_URL='postgresql://.../source'
export DATABASE_URL='postgresql://.../target'
python tools/transfer_existing.py
python tools/transfer_existing.py --execute --acknowledge-excluded-tables
python tools/transfer_existing.py --verify
```

The command compares captured columns, foreign-key behavior, constraints and
required indexes, rejects row security, triggers, same-schema connections,
incomplete target migrations and nonempty targets, then copies
rows in model dependency order. The dry run lists excluded source tables;
`--execute` requires `--acknowledge-excluded-tables` when that list is nonempty.
Foreign keys crossing from captured to excluded tables are refused. Execution
locks source and target tables, compares row digests, and carries over each
captured ID sequence state. `--verify` is read-only reconciliation of captured
rows and sequences after the copy; it fails if either side drifts. A second copy
refuses to run. Use a source write freeze and an isolated clone first; this
command does not synchronize writes made after its source snapshot. Excluded
tables, including Django's built-in auth and migration tables, and
application-specific jobs still need a separate cutover plan. SQLite source
databases do not have this transfer recipe. The FastAPI transfer requires source
and target schema parity; it does not apply Alembic migrations or copy
`alembic_version`, which appears in the dry run as an excluded table.

`adopt-existing` is a validation-only Goose baseline for a PostgreSQL schema
containing exactly the captured tables. It refuses extra tables, changed columns,
constraints, sequences, indexes, triggers or row security. PostgreSQL 18's
validated `NOT NULL` catalog constraints are accepted as column nullability;
unvalidated ones are refused. A standard Django
database also contains built-in tables, so direct in-place adoption is not the
qualified cutover path.

### What this profile leaves out

This profile retains `complete_backend=false`. Browsable HTML, form parsers,
router root/OPTIONS/format suffixes, framework-specific malformed-JSON and
unhandled-error bodies, host validation, arbitrary middleware, unrecognized
validators and full application cutover remain outside it. Passing fixture replay
does not qualify those paths or production deployment.
