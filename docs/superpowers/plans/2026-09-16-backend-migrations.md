# Backend Migration Implementation Plan

> **Execution:** implement in order using `superpowers:executing-plans`; each task
> follows failing regression → minimal implementation → verification → reviewed PR.
> Track implementation evidence in [the progress report](2026-09-16-backend-migrations-progress.md).
> Do not begin Go implementation before the Flask gate.

**Goal:** make DRF → Flask a deterministic whole-backend migration, then reuse its
verified contracts for Python framework → Go migrations.

**Architecture:** incrementally extract source behavior capture and portable IR into
a shared helper package. Keep synchronous Flask and asynchronous FastAPI emitters
independent. Generate only layers justified by the captured application.

**Tech stack:** existing Python/uv/pytest tooling; Flask, synchronous SQLAlchemy and
Alembic; Go with Fiber by default, selectable Fiber/chi/mux/Gin and pgx/Goose first.
Additional database layers require separate qualification.

**Spec:** [Backend migration design and current comparison](../specs/2026-09-16-backend-migration-design.md).

## Global constraints

- Preserve `sanka-extension/v1`, public extension IDs, installed locks, SDK boundaries
  and Apache-2.0 source headers. Version changed artifacts explicitly.
- Extension packages must not import each other or the Sanka runtime. The new helper
  package must not import the extensions. Generated target dependencies stay in the
  generated project, not the SDK.
- No customer database writes, local servers, publication or production deployment
  during implementation without the applicable explicit authorization. Use existing
  test clients, isolated fixtures and CI database services.
- Preserve source HTTP, authorization, data and side-effect semantics. Unsupported
  behavior fails completion; scaffolding is not a completed migration.
- No external architecture-reference identity, copied source, copied naming or copied
  folder layout in implementation, generated artifacts, documentation or prompts.
- Freeze effective source settings, dependency versions, generator/profile versions
  and formatting tools. No network resolution or model sampling during regeneration.
- Do not emit unused services, repository wrappers, interfaces, auth, DBs or middleware.
- Existing Flask defaults and project locks retain their semantics. Native SQLAlchemy
  is a new versioned profile before any default migration is considered.

## Delivery sequence

| Milestone | Tasks | Exit |
| --- | --- | --- |
| A. Evidence and shared foundation | 1–3 | Baselines captured; shared extraction preserves both converters |
| B. Complete Flask generation | 4–8 | Schema, runtime, auth, queries, validation and business behavior qualified |
| C. Flask release gate | 9–10 | Deterministic standalone output, parity, packaging and reviewed release evidence |
| D. First Go backend | 11–12 | Python → Fiber/pgx/Goose passes equivalent gates |
| E. Broader source/target matrix | 13–14 | DRF/FastAPI/Flask × Fiber/chi/mux/Gin qualified per capability |

Tasks are review boundaries, not a promise that a complex task fits one PR. Split
schema, security or scanner changes further when needed, retaining the same exit gate.

## 1. Establish the executable compatibility baseline

**Files:** both converter `tests/` directories; new `tests/backend_contracts/`;
`docs/converter-regression.md`; `scripts/converter_bench.json`.

- [ ] Run the current Flask/FastAPI tests on the exact starting revision and record
  failures separately from new work. Do not relabel existing failures as passing.
- [ ] Add a shared fixture inventory covering existing APIViews/forms, stock ViewSets,
  generic views, UUID/Boolean/large integers, nested CRUD, owner/member access and
  cursor/search/ordering behavior. Reuse existing synthetic fixtures before adding any.
- [ ] Record expected successful source responses and database effects. Include negative
  fixtures for unsupported middleware, hooks, raw SQL and custom migration operations.
- [ ] Record target-specific differences, including FastAPI's dropped suffix aliases.
  Flask must not lose its existing supported behavior while gaining FastAPI parity.
- [ ] Add a manifest that names each capability, profile, fixture and required check.

**Check:** extend the real protocol harness, not mocks. A baseline fixture must fail
when its expected source success is replaced by a matching source/target 404.

```python
assert result.source_status == 201
assert result.target_status == 201
assert result.source_database == result.target_database
assert result.target_database["created_records"] == 1
```

These assertions specify the proposed shared harness result, introduced in this task.
Test commands from repository root:

```sh
rtk proxy uv run python -m pytest packages/sanka-extension-drf-to-flask/tests -q
rtk proxy uv run python -m pytest packages/sanka-extension-drf-to-fastapi/tests -q
```

## 2. Add deterministic profile and architecture planning

**Create:** `packages/sanka-code-migration/pyproject.toml`, `LICENSE`,
`src/sanka_code_migration/{__init__,ir,policy}.py`, and `tests/test_policy.py`.
**Modify:** workspace `pyproject.toml`, lockfile, and `scripts/check_boundaries.py`.

**Interface:** `resolve_profile(ir: BackendIR, config: dict[str, object]) -> TargetProfile`.
`TargetProfile` carries target, ORM, dialect, schema mode, layout, layer decisions,
dependency/toolchain locks and reasons. `BackendIR` and `TargetProfile` are immutable
data contracts with strict versioned serialization, not framework objects.

- [ ] Write tests rejecting unknown fields, invalid types and unsupported combinations.
- [ ] Resolve `orm=sqlalchemy|django|none`, SQLite/PostgreSQL and
  `generation=auto|minimal|full`; retain existing update semantics until task 9.
- [ ] Implement the spec's exact service/repository and compact/modular rules.
- [ ] Record all resolved decisions in the reviewed plan; reject apply-time changes.
- [ ] Add dependency-boundary checks for the helper instead of loosening all checks.

```python
profile = resolve_profile(simple_crud_ir, {"orm": "sqlalchemy", "generation": "auto"})
assert profile.layout == "minimal"
assert profile.services == ()
assert profile.repositories == ()
assert resolve_profile(atomic_order_ir, {"orm": "sqlalchemy"}).services == ("orders",)
```

**Exit:** byte-identical canonical profile serialization for permuted unordered inputs;
semantic route/auth/middleware order retained; old locks do not change behavior.

## 3. Extract DRF capture without changing FastAPI behavior

**Create in helper:** `drf/{__init__,scan,serializers,access,parity}.py` as extraction
requires, and `tests/test_drf_capture.py`.
**Modify:** FastAPI `model.py`, `django_fastapi.py`, `metadata.py`, `parity.py`,
`access_contracts.py`, `nested_create.py`; Flask `adapter.py`, `native.py`,
`viewsets.py`; both package dependency declarations.

**Interface:** `capture_drf(root, settings_module) -> BackendIR`; converters adapt
the new IR to supported old artifact schemas until consumers migrate.

- [ ] Capture golden behavioral artifacts before moving code, excluding only execution
  metadata explicitly outside the content digest.
- [ ] Extract pure facts and recognizers in small pieces; keep target route rendering,
  framework request/response types and async stores in the owning extension.
- [ ] Preserve imports/class identity where published Python contracts require it.
- [ ] Include skipped/non-DRF routes and unsupported non-HTTP dependencies in inventory.
- [ ] Run live source introspection only through isolated workers with credential
  stripping, explicit settings inputs and resource limits; test an import with an
  attempted external side effect is blocked rather than executed on the operator host.
- [ ] Validate FastAPI unchanged after every extraction; run both converter suites.

```python
assert after.routes == before.routes
assert after.serializer_rules == before.serializer_rules
assert after.access_policies == before.access_policies
assert after.unsupported == before.unsupported
```

**Exit:** existing FastAPI and Flask regressions pass; neither extension imports the
other; captured facts no longer encode a FastAPI-only support verdict.

## 4. Capture complete schemas and generate database migrations

**Create in helper:** `drf/models.py`, `schema.py`, `tests/test_schema.py`.
**Create in Flask:** `src/sanka_extension_drf_to_flask/database.py`,
`tests/test_database_migrations.py`.
**Modify:** Flask planning and rendering in `adapter.py`.

**Interfaces:** `capture_schema(ir) -> SchemaIR` and
`render_database(schema, profile) -> dict[str, str]` produce model/config/Alembic files.

- [ ] Start with regression fixtures whose database-required fields are absent from
  serializers, plus FK, M2M, decimal, UUID, defaults and uniqueness constraints.
- [ ] Capture the complete supported model closure, deletion semantics and schema
  fingerprint; report custom types/triggers/data migrations that cannot be captured.
- [ ] Emit SQLAlchemy models and safe environment-based engine/session configuration.
  Preserve database dialect; support SQLite and PostgreSQL explicitly.
- [ ] Emit Alembic environment and deterministic initial revision for empty databases.
- [ ] Emit a separate adopt-existing preflight and reviewed baseline operation. Schema
  mismatch must prevent stamping, including missing indexes and constraints.
- [ ] Handle sequence/default state and schema-qualified identifiers. No runtime DDL.

```python
assert reflect(upgrade_empty(candidate)) == expected_schema
assert adopt_existing(schema_with_missing_unique).allowed is False
assert upgrade_existing_without_approval.database_writes == 0
assert render_database(schema, profile) == render_database(schema, profile)
```

**Exit:** empty-DB CRUD and existing-DB preservation pass on both dialects; future
revision upgrade is tested; unsupported historical effects have explicit dispositions.

## 5. Emit a native Flask application and preserve request contracts

**Create in Flask:** `application.py`, `routes.py` and
`tests/test_standalone_flask.py`, `tests/test_route_contracts.py`.
**Modify:** `adapter.py`, `viewsets.py`, generated file manifest.

**Interface:** `render_flask(ir, profile) -> dict[str, str]`; generated
`create_app(config=None)` owns config, engine/session factories and Blueprint registration.

- [ ] Add a failing test that installs only generated runtime dependencies and imports
  the app without Django, DRF, FastAPI or source modules present.
- [ ] Generate synchronous handlers and Blueprints; preserve URL order and converter
  semantics. Add generic CRUD view support from captured operations.
- [ ] Preserve HEAD/OPTIONS/405, negotiation, error envelopes, suffix aliases, custom
  lookup parameters and slash redirects; disable incompatible framework defaults.
- [ ] Retain recognized APIView and upload/form behavior through the selected profile;
  ORM-dependent carryover unsupported by SQLAlchemy must remain an explicit gap.
- [ ] Add environment config, request size bounds, connection cleanup, safe errors,
  logging and a documented production WSGI entrypoint. Extra health routes are opt-in.

```python
assert app_a is not app_b
assert client.open("/items/", method="OPTIONS").json == source_options
assert client.open("/items/1/", method="TRACE").status_code == 405
assert source_framework_imports(candidate_process) == set()
```

**Exit:** all existing Flask route fixtures plus generic CRUD pass against native
SQLAlchemy where qualified; retained-Django mode still passes its previous tests.

## 6. Preserve validation, nested writes and domain operations

**Create in helper:** `validation.py`, `operations.py` and their focused tests.
**Modify in Flask:** `native.py`, `native_runtime.py`, `model_runtime.py`, emitter.
**Add Flask tests:** `test_validation_contracts.py`, `test_transaction_contracts.py`.

- [ ] Capture field/default/error contracts separately from database mappings.
- [ ] Implement the union of current supported Flask and FastAPI field behavior,
  including missing/null/blank, read/write-only, partial updates and nested errors.
- [ ] Lower only recognized source business operations; preserve existing supported
  pure Python functions where their transitive imports are valid for the profile.
- [ ] Generate services for real orchestration/reuse. Share the transaction across
  every write; preserve source savepoints and propagate commit failures.
- [ ] Catch unique races using DB constraints; test simultaneous duplicate submissions.
- [ ] Leave unknown hooks/signals/external side effects as blocking gaps.

```python
assert patch({"enabled": False}).json["enabled"] is False
assert patch({}).json["enabled"] == previous_enabled
assert nested_failure.database_after == nested_failure.database_before
assert sum(r.status_code == 201 for r in concurrent_duplicates) == 1
```

**Exit:** source/target responses and persisted effects match for success and failure;
simple CRUD emits no service or pass-through repository files.

## 7. Implement auth, permissions and middleware as behavior contracts

**Create in Flask:** `security.py`, `middleware.py`, `tests/test_security_contracts.py`.
**Modify:** shared access capture, Flask routing and database session integration.

- [ ] Port qualified Token, owner/member and parent-scoped policies first. Preserve
  existing Basic/header-auth cases and authenticator precedence.
- [ ] Add unauthorized, invalid credential, inactive user, cross-owner, cross-member,
  membership-removal and foreign-parent write tests before widening recognition.
- [ ] Implement stock session/CSRF support as a separately tested capability, including
  session signing/storage and cookie behavior. Until it passes, report a blocking gap.
- [ ] Map supported middleware settings in source order: host/security headers,
  redirects, sessions/CSRF and CORS. Unknown configurations fail qualification.
- [ ] Cover auth/permission interaction with OPTIONS, method errors and object lookup.
- [ ] Preserve password/token formats; do not add replacement authentication schemes.

```python
assert stranger.get(owner_resource).status_code == expected_source_status
assert forbidden_write.database_after == forbidden_write.database_before
assert session_write_without_csrf.status_code == 403
assert invalid_token_options.headers == expected_source_headers
```

**Exit:** no privilege widening or denied-write side effects; each unsupported
auth/middleware configuration is identified before apply, not silently omitted.

## 8. Preserve filtering, ordering and pagination

**Create in helper:** `queries.py`.
**Create in Flask:** `listing.py`, `tests/test_listing_contracts.py`.
**Modify:** shared DRF capture and SQLAlchemy query emitter.

- [ ] Reuse captured search/ordering/cursor rules; add stock page-number and limit-offset.
- [ ] Support explicit bounded django-filter field/operator combinations; reject custom
  filter methods and unsupported lookups until covered by real differential fixtures.
- [ ] Apply permission scoping before pagination/counts. Use SQL filters and bounded
  fetching; do not scan an entire table in Python for every page.
- [ ] Preserve repeated query parameters, invalid values, links, empty pages, ties,
  forward/reverse cursors and source collation behavior per database dialect.

```python
assert all_pages(target, filters) == all_pages(source, filters)
assert foreign_member_list.json == source_foreign_member_list.json
assert rows_fetched_for_bounded_page <= expected_fetch_bound
```

**Exit:** source/target list bodies, ordering, counts and links match on SQLite and
PostgreSQL; unsupported collation/lookup behavior cannot pass through as supported.

## 9. Deterministic output, generated tests and safe regeneration

**Create:** `tests/backend_contracts/test_determinism.py` and
`packages/sanka-extension-drf-to-flask/tests/test_regeneration.py`.
**Modify:** helper serialization, Flask plan/apply/test, generated project renderer.

- [ ] Generate application tests from captured contracts, plus independent differential
  scenarios. Reuse `sanka-drf-replay`; extend it without adding framework dependencies.
- [ ] Add PostgreSQL fixture replay alongside existing SQLite replay: independent
  databases, test-target validation, schema/data/sequence comparison and guaranteed
  cleanup. Database clients run in the fixture interpreter, not the stdlib helper.
- [ ] Lock generated dependencies; include chosen profile, schema and artifact digests.
- [ ] Generate in independent roots with different hash seeds, time, environment order
  and source discovery order; compare every portable file byte.
- [ ] Test changed source/config/toolchain invalidation; review hashes bind all choices.
- [ ] Track generator-owned file hashes; support update only when ownership checks pass.
  Preserve hand edits and removed-file conflicts, and write atomically.
- [ ] Harden path/symlink traversal and prevent source/output self-inclusion in digests.

```python
assert generated_files(run_a) == generated_files(run_b)
assert apply(changed_config, reviewed_plan).outcome == "error"
assert update(edited_generated_file).outcome == "conflict"
assert edited_generated_file.read_bytes() == original_user_edit
```

**Exit:** generated projects are reproducible and useful without the source checkout;
boot/tests/verification are separate machine-readable stages.

## 10. Qualify and release Flask before beginning Go

**Modify:** `.github/workflows/ci.yml`, `scripts/run_converter_bench.py`,
`scripts/build_release.py`, `scripts/check_release_artifacts.py`, package manifests,
marketplace hashes and converter READMEs. Inspect release scripts before changing pins.

- [ ] Add native Flask SQLite/PostgreSQL acceptance and supported Python/Django/DRF
  source-version jobs. Run the installed wheel, not only workspace editable imports.
- [ ] Test concurrent requests, rollback, connection cleanup, bounded bodies and SQL
  query limits. Generated tests must detect deliberate auth/validation regressions.
- [ ] Extend exact-SHA private benchmark coverage to the real native SQLAlchemy output;
  keep private fixtures/results private and retain existing FastAPI projection checks.
- [ ] Run the broad workspace checks and release-artifact validation with bounded local
  resource use; integration tests skip only when their documented service is absent.
- [ ] Publish the shared helper before dependent extensions, following reviewed release
  tooling. Test installation from locked marketplace wheels and rollback to prior locks.
- [ ] Update hosted consumer pins only in separately reviewed downstream work.

```sh
rtk make check
rtk make build-release
```

**Mandatory Go entry gate:** all in-scope Flask capabilities above are qualified,
FastAPI has no regressions, native generated output runs without source dependencies,
schema lifecycle works on both supported dialects, determinism passes, and unresolved
gaps are honest blockers. A lower readiness percentage is not a waiver of this gate.

## Coordination update: Python to Golang (2026-09-17)

This update supersedes the original chi-first, DRF-only sequence in tasks 11–14.
The extension is `python-to-golang`, with Fiber as default and selectable Fiber,
chi, Gorilla mux (`mux`) and Gin. Sources are DRF, FastAPI and Flask. The existing
experimental implementation covers bounded static capture, literal GET, PostgreSQL
schema generation and ordered reads. Request-driven string filters landed in PR70 at `5842af1`. These bounded
capabilities do not establish complete backend qualification.

The TypeScript-to-Rust team proposed the ownership below. This is the intended
coordination plan, not evidence that cross-team agreement or shared code has landed.

| Work | Proposed owner | Python-to-Golang responsibility |
| --- | --- | --- |
| `packages/sanka-http-replay`, stdlib orchestration and compiled-target protocol | TypeScript-to-Rust team | Review contract; supply Python/Go adapters and parity fixtures |
| Hosted recipe table and checksum-verified toolchain layers in `sanka-api/runners/developer` | TypeScript-to-Rust team | Supply Go recipe, target selection, toolchain and vendored module requirements |
| CLI selected-target forwarding in `sanka` | TypeScript-to-Rust team | Normalize aliases, reject conflicts and verify lifecycle persistence |
| Shared IR and PATCH representation | Joint contract review | Preserve Flask behavior and qualify Go lowering before enabling writes |
| Go capture, generation and backend semantics | Python-to-Golang work | Continue independently; adopt shared infrastructure after compatibility checks |

Do not create a competing generic replay package or hosted recipe framework.
Keep existing Go replay until its replacement passes the same cases. Mobile
SwiftUI/Compose verification remains separate from HTTP replay. Helpers must not
import extensions or the Sanka runtime; publish and pin helpers before dependent
releases. Hosted delivery remains a separately reviewed downstream change.

### Shared integration gates

- [ ] CLI: forwarding is implemented in [sanka PR119](https://github.com/sankaHQ/sanka/pull/119)
  (open, CI green when checked on 2026-09-18); adopt and verify after merge.
  Forward `--to` into `configuration.target`; map it to `target_framework`
  and reject conflicting explicit values. Adding `target` to the allowlist alone
  still defaults generation to Fiber. Default to Fiber only when neither selects
  a framework. Persist the resolved target in the hashed plan and apply/test/verify.
  Test `--to chi`, defaults, matching aliases, conflicts and invalid values, plus
  regression coverage for existing extensions before enabling global forwarding.
- [ ] IR: `TargetProfile` currently accepts only Flask and `EffectiveInputs` requires
  that exact profile type. Generalize through a shared reviewed change, preserving
  existing Flask validation, serialization and hashes for unchanged inputs; version
  incompatible changes. Reuse `VersionPin` without weakening pin validation.
- [ ] PATCH: agree on absent/null/false/zero/empty-string distinctions, integer and
  decimal precision, time values, transaction and error contracts before rendering
  writes. Preserve source-specific validation. Task 11 is not yet a completed
  language-neutral PATCH contract.
- [ ] Replay: version the scenario/observation protocol, starting from hosted
  `sanka-verify.json` compatibility and explicitly defining `sanka-observed.json`.
  Include method/path/query/headers/body, ordered setup, source expectations,
  status/headers/body and database effects. Preserve numeric precision, missing/null,
  existing malformed/repeated-query behavior, hosted limits and coverage failures.
- [ ] Database isolation: reuse PostgreSQL clone handling from `sanka-drf-replay`
  through a shared helper boundary. Keep framework/driver dependencies in runner
  environments. Verify independent seeding, failure cleanup and protection against
  accidental customer-database use. Current Go public replay expects prepared
  databases; it does not clone or seed them or establish write parity.
- [ ] Go adapter: preserve exact toolchain/lock checks, bounded execution/output,
  credential-safe failures, source/candidate snapshot checks and stale-report
  invalidation. Run existing Go replay regressions before replacing its runner.
- [ ] Hosted: distinguish extension identity, source runner and target profile in
  the recipe table. Pin checksum-verified Go toolchain and vendored modules, with
  no implicit build downloads. Preserve existing Flask/FastAPI behavior and keep
  generated-project dependencies outside the SDK/runtime environment.

Shared infrastructure delivery must not block independent Go capture/rendering
work. Its acceptance gates must pass before claiming adoption or hosted readiness.

## 11. Complete the language-neutral operation contract and Go profile

**Create in helper:** `operations_go.py` only if Go lowering needs a separate unit;
otherwise extend `operations.py`; add `tests/test_go_semantics.py`.
**Extend:** `packages/sanka-extension-python-to-golang/` with its existing subprocess
lifecycle, target profile validation and tests. Register only qualified source/target pairs.

- [ ] Extend the existing IR for concrete cross-language needs: absent/null fields,
  typed decimal/integer/time values, transaction operations, context and error contracts.
- [ ] Use `target_framework=fiber` by default, `database_layer=pgx`, PostgreSQL and Goose
  explicitly. Resolve and pin current compatible Go/module/tool versions once.
- [ ] Write source/target tests for false/zero/null PATCH, big integers, decimals,
  timezone changes, password hashes and deterministic names before rendering code.
- [ ] Reject unsupported Python dynamics and arbitrary source code; do not invoke a
  free-form translator to fill holes in deterministic output.

```python
assert lower_patch({}).has("enabled") is False
assert lower_patch({"enabled": False}).value("enabled") is False
assert lower_patch({"enabled": None}).is_null("enabled") is True
```

**Exit:** all representation rules have fixtures; no Go framework types enter shared IR.

## 12. Deliver Python → Fiber as a complete backend and adopt shared replay

**Extend:** existing `src/sanka_extension_python_to_golang/` capture, generation
and tests; retain all three-source/four-target bounded-read regressions.
**Adopt:** shared scenario format and compiled Go adapter under the ownership and
compatibility gates above. Fiber is the first complete-backend qualification gate.

- [ ] Generate models, request validation, handlers, parameterized SQL,
  pgx pooling, Goose migrations, configuration, tests and documented build/run commands.
- [ ] Generate conditional services/repositories using the same evidence rules as Flask.
  Domain functions take `context.Context`; constructors wire concrete dependencies.
- [ ] Port qualified access/query/middleware contracts with source-compatible errors.
- [ ] Handle cancellation, transaction rollback, commit errors, pool closure, HTTP
  timeouts, panic recovery and shutdown. Avoid storing request context after completion.
- [x] Support empty-schema setup and verified adoption, never automatic migration on boot.
- [ ] Run language-neutral requests against Python source and Go target with independently
  seeded PostgreSQL databases. Native Go must not shell out to Python at request time.
- [ ] Compare generated files across independent locked runs.

Generated-project checks:

```sh
go test ./...
go test -race ./...
go vet ./...
go build ./cmd/api
```

**Exit:** the Flask production qualification categories also pass for this exact Go
profile, including database effects and authorization, not just compiling binaries.

## 13. Qualify complete backends across chi, mux and Gin

**Extend:** existing target generation and profile-specific contract tests.
All four frameworks already have bounded support; qualify additional behavior
per target. Reuse domain/query emission rather than forking it.

- [ ] Qualify chi, mux and Gin behind the same contract corpus; verify binding, route conflicts,
  middleware order, HEAD/OPTIONS and response differences explicitly.
- [ ] Retain Fiber-specific qualification for request-context lifetime, cancellation, body parsing and
  native test adapter. Never assume net/http middleware is directly interchangeable.
- [ ] Add sqlc or GORM only as separately qualified, explicit database alternatives; preserve precision,
  null/default/update semantics and transactions. Do not use AutoMigrate as migrations.
- [ ] Keep Goose schema ownership consistent across Go persistence profiles.
- [ ] Publish a support matrix; each offered combination needs its own CI acceptance.

```python
assert contract_results("chi", "pgx") == expected_contract
assert contract_results("gin", "pgx") == expected_contract
assert contract_results("fiber", "pgx") == expected_contract
assert contract_results("chi", "gorm") == expected_contract
```

**Exit:** each enabled combination passes; other frameworks and database layers are
rejected by configuration validation until implemented, not advertised as generic support.

## 14. Expand DRF, Flask and FastAPI source coverage

**Extend:** existing source capture and fixtures; extract shared scanner code only
when reuse requires it. Bounded support already exists for all three sources.
**Modify:** Go capability matrix as each additional behavior is qualified.

- [ ] DRF scanner: explicit strict BaseSerializer validation is bounded support;
  expand field-based serializers, views/routers, permissions and application
  settings without silently dropping unsupported behavior.
  - [x] Qualify flat conventional `Serializer` scalar fields, coercion, ignored
    unknown keys, nullable values and PATCH omission across all four Go routers.
  - [ ] Qualify `ModelSerializer`, ViewSet/router wiring, field-shaped errors,
    permissions and application settings.

- [ ] Flask scanner: application factories, Blueprints, installed ORM/schema libraries,
  request hooks, auth decorators, error handlers and registered routes.
- [ ] FastAPI scanner: APIRouters and explicit flat strict Pydantic write schemas are
  bounded implementations; general Pydantic models, dependencies/security dependencies,
  async/session lifecycle, middleware and exception handlers.
- [ ] Preserve source framework semantics rather than normalizing everything to DRF
  defaults, especially status/error bodies and dependency execution order.
- [ ] Capture source tests as evidence; add independent business-effect fixtures.
  Recognize third-party integrations individually and report unknown ones as gaps.
- [ ] Require a complete vertical slice to the Fiber profile before
  qualifying the same additional behavior on chi/mux/Gin. Run every advertised combination in CI.
- [ ] Repeat wheel installation, artifact determinism and exact-commit release gates.

```python
assert replay("flask", "fiber", scenario).matches_source
assert replay("fastapi", "fiber", scenario).matches_source
assert unsupported_custom_dependency.completion_allowed is False
```

**Exit:** source framework × target framework × persistence support is backed by
specific passing evidence. Additional combinations are separate incremental work.

## Execution checks and progress reporting

For each task, keep its checkboxes open until its own exit criteria pass. Report the
source SHA, selected profiles, tests run, unsupported behavior, and release state.
Do not equate a generated project, passing lint, or a merged PR with a verified migration.

Before broad local tests, inspect other pytest processes and memory pressure. Use
focused tests first and no unbounded xdist. Do not run customer migrations or start
local services to satisfy a test; use authorized services or CI.

The progress report records implementation tests and remaining qualification gaps.
Keep individual task exits open until their full criteria pass; partial implementation
and local fixture evidence do not satisfy service, publication or production gates.
