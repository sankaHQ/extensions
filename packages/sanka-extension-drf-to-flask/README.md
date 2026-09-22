# DRF to Flask

An Apache-2.0 migration extension using `sanka-extension/v1`. It scans Django's
resolved DRF routes, creates a deterministic reviewed plan, and emits a native
Flask target and a machine-readable gap inventory. The existing Django ORM profile
remains the default; `orm=sqlalchemy` selects independent database and schema ownership.
It never imports the Sanka runtime or the FastAPI extension.

## Standalone SQLAlchemy profile

Set `orm` to `sqlalchemy` when planning. `generation=minimal` keeps the application
compact, `generation=full` places it in a `backend` package, and `generation=auto`
selects from captured application boundaries. Use `--extension-config` for profile
settings the installed CLI does not expose as flags:

```json
{
  "orm": "sqlalchemy",
  "generation": "auto",
  "database": {"dialect": "preserve", "schema_mode": "adopt-existing"},
  "layers": {"services": "auto", "repositories": "auto"},
  "completion_policy": "strict"
}
```

The generated project owns a Flask application factory, synchronous SQLAlchemy
sessions, model tables, explicit Alembic migrations, dependency pins, configuration
example, and runnable database/factory tests. Importing or booting the app never
creates a table or runs a migration. Models are captured independently of serializers,
including fields omitted from the HTTP interface. SQLite and PostgreSQL are separate
profiles; changing the database dialect is rejected.

For an empty destination, run the generated Alembic baseline explicitly. For an
existing destination, run its read-only `database.check_schema(engine)` first, then
call `database.adopt_existing(engine, reviewed_schema_hash)` only after reviewing the
schema hash. Adoption refuses mismatches and preserves data; it does not rerun Django
data migrations. Unsupported schema, application hooks, or HTTP behavior block native
generation instead of producing successful placeholders.

`architecture.json` explains layout and layer decisions. `migration-inputs.json`
records effective inputs; `generated-files.json` records generated content hashes.
The same captured source, configuration, and pinned generator/dependencies produce
the same files across checkout locations. Existing outputs and hand edits are never
overwritten. The reviewed plan also binds the local output location, so its review
hash intentionally differs when that location changes.

The standalone profile currently recognizes JSON CRUD and generic views, Token/owner
permissions, atomic nested writes, stock page-number/limit-offset/cursor pagination,
search and ordering, bounded stock django-filter fields, and stock Common/Security/XFrame middleware. Recognized database
delete policies execute in the request transaction. Each supported contract has
source/target response and database-effect checks in the converter test suite.
Token/owner, conditional records, pagination, filtering and nested-write fixtures also
run against independent PostgreSQL schemas in CI. Built Flask and shared-helper wheels
are installed and tested separately from their editable source packages. Generic `verify` replay supports SQLite and explicitly configured PostgreSQL
with isolated seeded database clones; see the [replay configuration](../sanka-drf-replay/README.md#postgresql-replay).

Stock `DjangoFilterBackend` supports qualified automatic scalar filters, including
repeated parameters, CSV inputs and detail filtering. Custom filtersets, related/date
filters and unsupported lookups remain blocking gaps. Filter errors preserve field
order and source validation messages. JSON body-limit behavior is captured from the
source parser. Enforced limits preserve the source error response without writes;
a source parser that accepts the request does not acquire an invented rejection.

Stock database sessions are supported when every migrated view uses exact
`SessionAuthentication` with `IsAuthenticated`, the stock database session backend,
JSON session serialization, timezone-aware expiry (`USE_TZ=True`), `ModelBackend`,
and the captured session/auth/CSRF
middleware. Existing signed sessions, key fallback rotation, expiry, CSRF tokens,
Origin/HTTPS Referer checks and cookie effects are preserved by the qualified
contract. Signing keys are explicit target environment inputs, never generated
values. Login/logout endpoints are not invented. Mixed authentication policies,
custom user lookup/hash behavior and unrepresented settings remain blocking gaps.

| Authentication profile | Retained Django ORM | Standalone SQLAlchemy |
| --- | --- | --- |
| No authentication / AllowAny | Qualified stock ViewSets | Qualified JSON contracts |
| Default Session then Basic, no middleware | Basic works; session cookies are inert | Blocking gap |
| Token with captured owner/member permissions | Outside retained ViewSet scope | Qualified captured contracts |
| Stock database session with IsAuthenticated and CSRF | Middleware remains a gap | Qualified bounded contract above |
| Custom or combined authenticators | Only the documented APIView subset | Blocking gap |

Explicit scalar validators whose limits, messages or error ordering cannot be
represented block conversion. Native decimal fields with non-integer bounds,
custom rounding, localization or normalized output also block conversion rather
than silently changing validation or response values.

Basic or combined authenticators, browsable HTML, multipart/form parsing, custom middleware,
signals, arbitrary serializer hooks, and historical RunPython/RunSQL migrations remain
blocking gaps in this profile. Custom view carryover supports the recognized
conditional-response recipe or strict literal response transformations after the
matching stock CRUD call. The latter can set ordinary response headers, choose a
2xx status (except 204/205), and wrap `response.data` with JSON literals. PATCH
preserves the source update/partial-update order; validation and permission errors
bypass successful response transformations. Custom parent method chains,
renderer-owned/transport headers, dynamic expressions, request mutation and arbitrary
custom actions remain gaps. `USE_TZ=False` with
automatic timestamp fields is also blocked until its whole request contract is qualified. Existing APIView/form conversions remain available in
the Django ORM profile below. Selecting SQLAlchemy never silently drops these features.
Source introspection imports application code; run scans only in a trusted source
execution environment. Static inventory flags recognized raw SQL, network, email and storage operations,
alongside lifecycle hooks and data migrations; it does not resolve arbitrary dynamic
Python behavior and is not a sandbox for untrusted Python.

Generated dependencies are resolved in checked-in `uv.lock` profiles and hash-locked
pip requirements. The generated README includes environment setup, migration commands,
factory tests and a production WSGI command. These runtime dependencies are installed
in the destination project, independently of the extension's own environment.

This profile is qualified by its explicit supported contracts, not by the size
of an application. Run independent source/target scenarios and database-effect checks
before cutover. A successful syntax or startup check is not production qualification.

## Existing Django ORM profile

This alpha converts recognized JSON APIView handlers, configuration-only
APIView inheritance, and the stock ModelViewSet JSON scope described below. It preserves JSON parsing, isolated Django ORM modules,
transaction blocks, and framework-independent project functions whose transitive
imports stay inside the permitted ORM/stdlib boundary.

The APIView envelope also includes plain serializers with CharField/IntegerField
validation and self-independent object validation, plus a single self-independent
header authenticator returning a user/token pair or raising AuthenticationFailed.
That authentication subset requires AllowAny and UNAUTHENTICATED_USER=None;
session authentication, multiple authenticators, custom permissions, throttling,
custom dispatch, serializer saves/nested fields in APIView handlers, and middleware
remain manual gaps. ModelViewSet support has its own bounded serializer/authentication
checks described below.
No DRF classes are imported by the generated serving process. Recognized HEAD,
OPTIONS, method rejection, JSON content negotiation, and conditional Allow headers
are generated alongside handlers. Readiness counts converted method/path pairs,
not placeholders, and does not certify untested behavior.

Use `sanka/drf-to-flask` through the CLI marketplace/project lock. Follow
`scan → plan --to flask --strategy native --generation minimal → apply → test → verify`.
Apply requires the reviewed **core** plan hash. The output is an overlay: retain the
original ORM modules and dependencies on the target Python path and install Flask
in the target environment. `test` checks the unchanged generated files compile and
the native Flask app boots without DRF. It rejects plans with manual gaps; it does
not claim HTTP or database parity. `verify --scenarios` performs that comparison
with isolated fixtures. For an already repaired candidate, preserve the changes and
use scenario verification instead of regenerating a plan just to rerun its test gate.

Install source dependencies in the project's `.venv`. The locked extension runs
with that environment's Python while retaining its reviewed code and protocol.
The environment must use the same Python major/minor version as the extension.
Missing dependencies report the interpreter that needs them; Django is not
installed into the CLI's environment. Generated output is excluded from the
reviewed source hash, while source edits still require a new plan.

Unsupported mapped routes still return 501 and remain listed in migration-gaps.json.
Those are incomplete migrations. Repair explicit gaps only; prefer reusing domain
functions and extension replay over recreating them in a model-written test harness.

Apply rejects changed source, tampered plans, changed output locations, symlinks,
existing output directories and collisions with original source files. It does
not overwrite earlier repairs. `bench_candidate` selects a new overlay directory
without copying or modifying the original application.

The release manifest is staged for the next extension release; a repository PR is
not a published wheel. Local development: `uv sync --all-packages` and
`uv run pytest packages/sanka-extension-drf-to-flask/tests`.


## Differential replay

`verify` with `scenarios` compares the live DRF source and a repaired candidate,
without requiring a reviewed plan. Configure the source's settings to read an
isolated SQLite path from `SANKA_TEST_DB` (or pass `--db-env`). Replay refuses
settings that point elsewhere, before running migrations or seeds.

```sh
sanka verify . --scenarios public-tests/scenarios.json --candidate candidate \
  --settings config.settings --db-env SANKA_TEST_DB --seed seed.py --json
```

The response gives counts, up to 20 failing scenario messages, and `report_path`.
The report artifact contains all results; open it only when a failure needs more
detail. Use `--source-python` and `--candidate-python` for separate source/target virtual
environments. All fixture databases are temporary and removed after replay.
Omit a scenario's `body` for no bytes, use `body: null` for JSON null, or
`body_base64` for raw input. Capture required response headers explicitly.
The independent benchmark's acceptance and native-compliance gates still apply.

Unmatched URLs use the source Django built-in 404 page, rendered as a native Flask
response. Custom `handler404`, `404.html` templates, and debug error pages still
require manual adaptation and differential verification.

### Form and upload parsing

`apply` includes `sanka_form.py`. Recognized APIView handlers with stock FormParser
or MultiPartParser use it automatically. For routes that still need manual
adaptation, use `parse_form(request)` to obtain source-compatible fields and
Django UploadedFile objects; catch `FormError` and retain its `detail` and `status`.
Flask still owns routing and serving; the helper does not import DRF.

Compatibility includes the source Django parser's boundary-token behavior, which
can truncate file contents. `verify` reports these mismatches as
`multipart_boundary_parity`, with repair guidance and differing saved-file sizes.
A parity pass does not establish that the source preserves all uploaded bytes.
Custom view/rendering behavior remains an explicit migration gap.


### ModelViewSet JSON APIs

The native target also supports stock `ModelViewSet` CRUD with Django ORM,
`SimpleRouter`/`DefaultRouter` routes, JSON format aliases, and the API root.
Supported `ModelSerializer` fields are character, integer, decimal, choice, and
nested model lists. Explicit, self-independent nested `create` and `update`
methods are lowered with their ORM writes and transaction boundaries intact.
Validation messages, uniqueness checks, read-only fields, partial updates, and
nested decimal output are captured from the source environment.

This target is a JSON API: the DRF browsable HTML interface and form uploads are
not generated for ViewSets. That scope change is included in the reviewed plan.
Django models and their dependencies remain necessary. Empty authentication or
the default Session/Basic combination with no middleware is supported; Basic
credentials are still authenticated. Custom permissions, serializer hooks,
filtered querysets, pagination, middleware, and unsupported global settings
remain explicit gaps and cannot pass the generated-app check.

`test_model_viewsets.py` compares source and generated JSON responses and database
counts for nested CRUD, rollback, validation failures, and Basic authentication.
It also checks that the Flask serving process imports no DRF modules. These tests
qualify this bounded scope; they do not establish parity for arbitrary projects.
