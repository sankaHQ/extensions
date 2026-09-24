# Sanka DRF-to-FastAPI extension

This independently executable extension scans Django REST Framework projects and plans,
generates, tests, and verifies FastAPI migrations through the `sanka-extension/v1` JSON
subprocess contract.

It writes one JSON response to stdout. Diagnostics are written to stderr.

## Source Python environment

The source application's dependencies belong in `<project-root>/.venv`, separately
from the CLI and the installed extension. For example:

```sh
uv tool install --python 3.12 sanka-cli
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
sanka extension add sanka/drf-to-fastapi
sanka scan .
```

The extension runs with that project's Python, loads its reviewed implementation
from the locked installation, and uses the project's environment for Django and
application imports. It does not use an unrelated activated environment or require
`PYTHONPATH`. The source and extension interpreters must have the same Python
major/minor version, so locked binary dependencies remain compatible. An incomplete
`.venv` or a version mismatch returns a protocol error with a recovery command.
Without a project `.venv`, execution retains the caller's existing environment.
On Windows, use `.venv\Scripts\python.exe` for the requirements command.

## Swagger UI

Planning keeps Swagger UI at `/docs` enabled by default. Use
`sanka plan . --to fastapi --no-swagger-ui` to disable that page, or choose
Disabled in the TUI Plan configuration. The reviewed plan controls generated
output in both native and compatibility modes. OpenAPI at `/openapi.json` and
ReDoc remain available.

## Parity notes

Every scanned route carries `parity_notes`: facts about the exact behavior of the
source application that a port must reproduce, derived from the live Django and DRF
classes rather than guessed. Families: `routing` (allowed methods, 405/OPTIONS bodies,
missing-object and trailing-slash behavior), `auth` (authenticator order, 401/403/404
ordering, exact token/session/basic failure details, CSRF), `conditional` (ETag and
304/412 logic and the operations that call it), `pagination`, `ordering`, `filtering`,
`multipart` (parsers and file-field rules), `uniqueness`, `nullability`, `messages`
(exact per-field validation strings), and `overrides` (project methods and the literal
strings they emit). Notes appear in `.sanka/scan.json` (schema 5), `plan-*.json`
(schema 4), the gap report, and the generated manifest's `unsupported_routes`. A family
that cannot be derived reports `SANKA_DRF_PARITY_UNAVAILABLE` instead of failing the scan.

`GAP-REPORT.md` lists each identical source fact once, with `G1`, `G2`, … references
on the routes where it applies. Different messages or source locations remain
separate. This reduces repeated reading without dropping route coverage or
changing the full JSON artifacts. Reuse generated helpers where they match the
source, then verify the remaining behavior against the source application.

## OPTIONS and 405 parity

Generated native apps answer `OPTIONS` with the exact `SimpleMetadata` body DRF would
send — view name, description, renderer and parser media types, and the `actions`
field map for POST/PUT when the caller passes the permission checks (PUT only when the
object exists and, for owner-restricted views, belongs to the caller). The scan captures
the anonymous and the authorized variants from the installed DRF; the runtime chooses
between them per request. Unsupported methods answer DRF's `405 {"detail": "Method
\"X\" not allowed."}` with the `Allow` header in `http_method_names` order.

## Generic CRUD views

Stock `CreateAPIView`, `ListAPIView`, `RetrieveAPIView`, `DestroyAPIView`,
`UpdateAPIView`, `ListCreateAPIView`, `RetrieveUpdateAPIView`,
`RetrieveDestroyAPIView`, and `RetrieveUpdateDestroyAPIView` use the same native
CRUD generator as supported ModelViewSets. The scan maps each original HTTP method
to its CRUD operation and preserves `http_method_names` restrictions. Serializer,
authentication, pagination, queryset, and middleware checks still apply.

Only the actual stock DRF handler functions qualify. Custom HTTP handlers,
authentication/permission hooks, and `as_view()` configuration overrides require
manual adaptation; they are never silently replaced with default CRUD. A view with
custom `get_queryset`, membership permissions, session authentication, throttling,
or serializer writes is not made compatible merely by inheriting a generic view.

### Remaining compatibility work

The generic-view foundation does not yet convert an application with all of these
patterns. Each next stage requires source/generated response and database parity:

1. Represent URL-scoped and member-scoped querysets, including parent/child object
   lookups; prove cross-member requests cannot read or mutate another member's data.
2. Preserve authentication order, token issuance, session/CSRF behavior, and rate
   limits, including anonymous, invalid-token, forbidden, and throttled responses.
3. Carry over membership additions/removals, serializer context writes, duplicate
   validation, computed fields, and transaction/rollback behavior.
4. Translate configured CORS and static-file middleware behavior explicitly. Do not
   remove middleware from the compatibility check without implementing its behavior.
5. Verify the complete repository flow on the released converter, then update the
   hosted worker's immutable converter pin and test GitHub pull-request delivery.

## List semantics and datetime fields

The native envelope covers DRF's generic list machinery: `CursorPagination` subclasses
that only set attributes (page size, ordering, parameter names; cursors, `next`/`previous`
links and the "Invalid cursor" 404 are reproduced statement for statement), `SearchFilter`
over text fields with the default, `^` and `=` lookups and DRF's smart term splitting, and
`OrderingFilter` with `ordering_fields` validation and the view's default ordering. A custom
`OrderingFilter` is accepted when probing its `get_ordering` against the stock filter
identifies a known tie-break idiom (appending the primary key). `DateTimeField` (ISO 8601
in and out, timezone-aware per the project's `TIME_ZONE`) is a native field kind. An
overridden viewset action now keeps only that route manual; the rest of the viewset stays
native.

## Verbatim carryover of overridden actions

An overridden viewset action (`list`, `create`, `retrieve`, `update`, `partial_update`,
`destroy`) and the helpers it calls on `self` are carried into the generated app unchanged
when they touch nothing but the standard library, DRF's `Response`/`status`/`Request`
names, and `super().<action>()`. They are emitted as a subclass of the runtime's
`CarryoverView` in `sanka_user_views.py`: `super()` reaches the generated handler (run on
the event loop while the carried code runs in a worker thread), DRF names resolve to shims,
and an error answered by the native handler surfaces as an exception exactly where DRF's
mixins would have raised. Actions that reach the ORM, `self.request`, `get_object`, or
other imports stay manual with their reasons. Generated apps also mirror Django's
Content-Length behaviour: the header is sent only when the source ran CommonMiddleware.


## Differential replay

`verify` with `scenarios` compares the live DRF source and a repaired candidate,
without requiring a reviewed plan. Configure the source's settings to read an
isolated SQLite path from `SANKA_TEST_DB` (or pass `--db-env`). Replay refuses
settings that point elsewhere, before running migrations or seeds. When the
candidate uses a different variable, set `candidate_db_env` in extension
configuration, for example `--db-env BENCH_DB_PATH --extension-config
'{"candidate_db_env":"SANKA_TEST_DB"}'`. Both variables are set to the same
isolated candidate database inside the candidate process; source requests still
use their own isolated database. The report records both variable names.

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

## DRF 3.17 large integer fields

The builtin `BigIntegerField` and model `BigAutoField` IDs retain 64-bit database
columns. The generated API preserves the field's `coerce_to_string` override or
`REST_FRAMEWORK["COERCE_BIGINT_TO_STRING"]` setting, including values beyond
JavaScript's exact integer range. Custom field subclasses still require adaptation.
Django 6 / DRF 3.17 source and generated HTTP/database parity runs in Linux CI;
this does not imply compatibility with every application using those versions.

PostgreSQL differential verification is opt-in through `database_backend` and
`postgres_admin_dsn_env`; see the shared [replay configuration](../sanka-drf-replay/README.md#postgresql-replay).
It uses dedicated temporary databases and does not change generation defaults.
