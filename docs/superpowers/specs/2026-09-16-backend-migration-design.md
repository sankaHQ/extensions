# Deterministic backend migration design

Status: proposed implementation design; no converter changes made.

## Scope and evidence

Deliver DRF → Flask first. Preserve the existing Flask capabilities, reach the
tested FastAPI capability envelope, then add the database lifecycle and release
gates needed for a standalone backend. Only after that gate passes, extend to
DRF, Flask, and FastAPI → Go, beginning with one complete target stack.

Inspection date: 2026-09-16. Local source revision:
`fa240f2e33a17c5ba6e13dff298f311daa4d1af2`. Remote main was fetched at
`78dbdc6b6e1b73c1486abb404e8858b2c9756ae8`; intervening changes do not alter
either converter. Findings below are source inspection, not a new test run.

Current implementation references, relative to repository root:

- Flask: `packages/sanka-extension-drf-to-flask/src/sanka_extension_drf_to_flask/`.
  `adapter.py` owns scan/plan/apply/test/verify; `viewsets.py` recognizes CRUD;
  `native.py`, `native_runtime.py`, `model_runtime.py`, and `form_runtime.py`
  own the current translation and serving helpers.
- FastAPI: `packages/sanka-extension-drf-to-fastapi/src/sanka_extension_drf_to_fastapi/`.
  `django_fastapi.py` owns scanning, planning, layouts, and generation;
  `model.py` owns IR; `native_async.py` owns model/store/runtime generation;
  `access_contracts.py`, `native_access.py`, `metadata.py`, `parity.py`, and
  `nested_create.py` contain reusable behavior capture and lowering rules.
- Verification: `packages/sanka-drf-replay`, both converter test directories,
  `scripts/run_converter_bench.py`, and `docs/converter-regression.md`.

## Current comparison

| Area | DRF → Flask today | DRF → FastAPI today | Required outcome |
| --- | --- | --- | --- |
| ORM choice | Django ORM only; other choices rejected | Tortoise, async SQLAlchemy, psycopg; benchmark projection retains Django | Native synchronous SQLAlchemy option; explicit retained-Django option; no silent fallback |
| Database setup | Overlay relies on source models, settings, dependencies | Generated connection helpers and dependencies; maps existing tables | Explicit runtime configuration, isolated testing, connection/transaction lifecycle, no source credential capture |
| Models | Original Django models retained | Generates supported model/column mappings | Complete supported schema graph independent of serializer exposure; constraints, relationships, indexes, defaults and deletion behavior |
| Validation | Bounded APIView serializers and ModelSerializer fields; nested writes in a restricted scope | Broader field IR, UUID/Boolean/large integer/datetime, supported nested/access contracts | Shared semantic rules; preserve coercion, errors, missing/null/blank and PATCH behavior |
| Routing | Recognized APIViews, stock ModelViewSets, API root and suffix aliases | ModelViewSets and generic CRUD views; selected overrides; format aliases disclosed as dropped | Blueprints/application factory; union of existing Flask and qualified FastAPI behaviors; no route silently omitted |
| Authentication | Restricted header auth for APIViews; bounded Basic behavior for ViewSets | Token and recognized owner/member policies; other patterns remain gaps | Preserve authentication order and object/list/write authorization; unsupported auth blocks completion |
| Middleware | Any configured middleware makes routes manual | Recognizes a bounded standard stack and emits selected HTTP security behavior | Explicit middleware contracts and ordering; security, session/CSRF, CORS and custom behavior independently qualified |
| Filtering/pagination | Rejected for ViewSets | SearchFilter, OrderingFilter and bounded cursor pagination | Same support, plus stock page-number/limit-offset pagination and bounded declared filters |
| Schema migrations | Retains Django migrations | Native path reuses tables; no full replacement migration history emitted | Alembic baseline for verified existing schemas and initial migrations for empty databases |
| Tests | Syntax/native boot gate plus differential replay; targeted nested CRUD tests | Broader parity tests, generated environment helpers and verification | Generated executable tests, independently seeded DB acceptance, PostgreSQL coverage and security regressions |
| Layout | Minimal overlay only | Minimal/full/update modes, config/logging/health and file hashes | Compact or modular native app selected by explicit rules; safe updates preserve manual edits |
| Business layers | Carries recognized ORM/domain code | Bounded carryover and native access/write contracts | Services only for recognized orchestration/reuse; repositories only where they provide a real persistence boundary |
| Determinism | Source and reviewed-plan hashes, stable rendering in bounded cases | Typed artifacts, hashes, ownership/update checks | Byte-identical output under a locked toolchain across directories, process seeds and execution order |

FastAPI is a regression baseline, not proof of arbitrary-project readiness.
Its documented converter benchmark uses a Django-ORM projection; that does not
prove the independently generated ORM target works. Flask acceptance must test
the actual selected backend, with source packages absent for native SQLAlchemy.

## Architecture decisions

Use a small shared migration-support library consumed by both extensions, while
keeping their subprocess entrypoints and public IDs unchanged. Extract existing
code incrementally; do not copy the FastAPI engine into Flask or make one
extension import the other. Keep framework dependencies out of the Extension SDK.

The alternatives are independent duplicated engines, which will drift, or a
universal compiler written before the Flask work, which delays useful results.
Choose shared behavior contracts with separate target emitters. Add a generic
instruction vocabulary only for source behaviors actually supported by a target.

Proposed package: `packages/sanka-code-migration/`, module `sanka_code_migration`.
It is a helper library, not a marketplace extension. It contains:

- Versioned, JSON-serializable backend IR and deterministic plan policy.
- DRF source capture, including provenance and explicit unsupported constructs.
- Pure validation, access-policy, schema and query descriptions.
- Target-neutral verification scenario definitions.

Flask owns synchronous Flask/SQLAlchemy emission. FastAPI keeps its async
transport and ORM adapters. Generated applications contain normal application
code and their selected runtime dependencies, never the converter package.
Do not convert async runtime source to sync by textual replacement.

### IR and completeness

Separate database schema from API representation. A model field excluded from a
serializer may still be required by a database constraint, ownership policy,
join, default, or write operation. Capture the reachable model graph, including
auth tables and association tables; inventory the rest of the application too.

The IR records routes and precedence, request/response schemas, model schema,
queries, transaction boundaries, authentication order, permission policies,
middleware order, configuration requirements, business operations, and tests.
Every element has a source location, stable identity, support status and reason.
Persist semantic facts rather than live Django objects, repr strings or memory
addresses. Bump artifact schemas and retain explicit readers for supported older
artifacts; never reinterpret an old reviewed plan under new defaults.

Inventory signals, management commands, tasks, external calls, storage backends,
raw SQL, database routers, custom fields and migration operations. Either support
them, explicitly retain them in a compatible target, or report a blocking gap.
An apparently complete HTTP surface is not a complete backend if these are lost.

Source import and introspection execute application code. Run them in the existing
isolated execution boundary with an allowlisted environment, no operator credentials,
bounded CPU/memory/time and isolated databases. Scan/plan must not contact production
services or execute migration/seed commands. Capture runtime settings through that
boundary and reject incomplete/dynamic facts rather than guessing them.

### Proposed configuration

These are proposed options, not currently available commands. Keep the existing
extension contract and `orm`, `generation`, `strategy`, `output`, and
`package_manager` integration where compatible.

```json
{
  "strategy": "native",
  "orm": "sqlalchemy",
  "generation": "auto",
  "package_manager": "uv",
  "database": {"dialect": "preserve", "schema_mode": "adopt-existing"},
  "layers": {"services": "auto", "repositories": "auto"},
  "completion_policy": "strict"
}
```

- New native Flask profile: synchronous SQLAlchemy 2.x, Alembic, SQLite and
  PostgreSQL initially. Exact validated package versions come from a versioned
  target profile, then are locked in the plan and generated dependency files.
- `orm=django`: preserve current behavior and source migration ownership,
  explicitly labeled as retaining Django. Existing project locks keep their
  behavior; changing the default profile requires a reviewed version transition.
- `orm=none`: selected only for an application proven to have no persistence.
- Reject unsupported combinations before generation. No placeholder ORM options.
- Database dialect is preserved; changing PostgreSQL to SQLite or migrating data
  between vendors is a different operation, never an automatic side effect.
- Strict completion is the default for the new profile. An explicitly requested
  partial scaffold can contain stubs but cannot pass completion verification.

### Determinism contract

Identical source content, normalized configuration, captured source settings,
source dependency lock, generator/profile versions and formatter/toolchain locks
must produce identical architecture decisions and file bytes. Record those inputs
as the migration input digest. Source/config alone cannot control unpinned tools
or environment-dependent Django settings, so all effective inputs must be frozen.

- Separate portable content digests from local execution paths and timestamps.
  Use source-relative provenance; execution output locations stay in the reviewed
  operation record without contaminating portable generated bytes.
- Sort unordered inputs, while preserving semantic order: route precedence,
  authentication, middleware, and declared ordering must not be sorted away.
- Fix UTF-8/LF output, identifier collision rules, import ordering, and tool versions.
- Migration IDs derive from canonical schema content and predecessor ID. No wall
  clock names or randomly generated revision identifiers.
- Dependency selection occurs once against a versioned profile. Regeneration uses
  the locked result; it must not resolve `latest` again.
- No model-generated code on the deterministic path. Assisted repairs are separate
  reviewed inputs, hashed and rerun through the same verification gates.
- Never embed database passwords, tokens, local absolute paths, secret settings,
  or mutable environment values in generated artifacts.

### YAGNI and project structure

Always generate an application factory; group routes by source module/domain using
Blueprints. Avoid a global singleton database session. Compact apps can use a small
`app.py`, `models.py`, `schemas.py`, and `database.py`; omit unused files.

`generation=auto` chooses modular layout only when there are multiple independently
owned route groups or a required service boundary. `minimal` and `full` remain
explicit choices. Full layout means organization, not permission to emit dead layers.

Generate a service when a recognized operation coordinates multiple writes under
one transaction, integrates a supported external boundary, or shares domain logic
between entrypoints. Generate a repository when that service needs a persistence
boundary or the source already has a supported reusable query component. Direct
simple CRUD uses the selected ORM/query API without a pass-through service and
repository pair. Decisions and their source evidence appear in the plan.

Never infer business meaning from file counts, line counts, class names, or an LLM.
Unrecognized domain logic is a gap, not justification to replace it with generic CRUD.

### Database ownership and transactions

Native SQLAlchemy output includes models, environment configuration, engine/session
lifecycle, and migrations. One unit of work owns commit/rollback; repository helpers
must not commit independently. Preserve source atomic sections and savepoints. Do
not silently make an originally non-atomic operation atomic without a contract change.

Capture PKs, UUIDs, precise decimals, timezone behavior, foreign keys, through tables,
uniqueness, indexes, checks, defaults, sequence identity and on-delete semantics.
Application-level duplicate checks do not replace unique constraints under concurrency.
Translate supported integrity errors without exposing SQL or credentials.

Two explicitly separate schema modes:

1. **Empty database:** generate an initial migration from the complete supported
   schema. Test upgrade, exact reflected schema, constraints and application CRUD.
2. **Adopt existing:** compare the existing schema fingerprint against the expected
   schema before creating a reviewed baseline/stamp operation. Stamp records a
   revision; it does not validate or create the schema. Generation never executes it.

Do not replay Django's historical data migrations against live data, call
`create_all()` during app startup, use ORM auto-migration as release management, or
drop/recreate existing tables. Unsupported RunPython/RunSQL, triggers, extensions
and custom fields require explicit disposition. A verified baseline can adopt their
result without claiming their history was translated. Destructive downgrade is not
a default rollback strategy; preserve backups and application rollback compatibility.

### HTTP, validation and security semantics

Preserve methods, custom actions, path converters, route precedence, slash behavior,
HEAD/OPTIONS/405, error envelopes, repeated query values, negotiation and configured
format aliases. Preserve Flask's existing upload/form support where applicable.

Validation must distinguish absent/null/blank/zero/false; preserve coercion, defaults,
read/write-only behavior, PATCH, nested errors, decimal precision and datetime zones.
Use captured rules, not generic library validation messages as a substitute.

Implement auth mechanisms and permissions independently: anonymous vs invalid
credentials, authenticator order, Basic/Token, session/CSRF where supported, ownership,
membership, parent-scoped lists and writes. Test denied requests for zero side effects.
Never replace existing authentication with a new JWT design or remove unknown middleware.
Custom backends, throttles and third-party middleware remain explicit gaps until qualified.

Translate middleware in order with a behavior matrix. Stock security headers,
host checks, slash handling, CORS and session/CSRF each need dedicated tests. Do not
infer that a recognized class name makes all its configuration supported.

Apply filters, permissions, ordering and pagination in source order. Push supported
filters/paging to SQL; do not load the whole table to paginate. Preserve source tie
behavior, or disclose and review a stable-ordering contract change.

## Go follow-on architecture

The first vertical slice is DRF → Go with chi + PostgreSQL + pgx/sqlc + Goose.
Then add Gin and Fiber transport adapters, followed by a separately qualified GORM
profile. Add Flask and FastAPI scanners against the same IR. Do not build nine
independent pairwise converters or expose unimplemented combinations as supported.

| Choice | Proposed use |
| --- | --- |
| chi | First HTTP target; standard net/http integration simplifies transport isolation |
| Gin | Next transport profile, same domain/query contracts |
| Fiber | Separate transport profile; explicit context lifetime and cancellation tests |
| pgx + sqlc | First PostgreSQL persistence profile: parameterized SQL and generated typed queries |
| GORM | Optional ORM profile after the SQL baseline passes the same schema/behavior suite |
| Goose | Versioned SQL migrations, including deterministic sequential filenames |
| Standard library | context, errors, encoding/json, log/slog, configuration parsing, testing and net/http where applicable |

Use the currently supported Go toolchain and validated module versions at profile
implementation time, then pin them in the profile and output `go.mod`/`go.sum`.
Do not claim a full framework/database cross-product is supported from one passing
combination. SQLite support for Go requires its own driver/profile and acceptance job.

Organize by feature, with `cmd/api`, `internal/config`, transport adapters, domain
operations and persistence. Services use `context.Context` and domain types; no
Flask, FastAPI, Gin or Fiber request objects leak into domain/persistence code.
Use constructors and small consumer-owned interfaces only at real boundaries.
No global resource bag, universal repository, interface for every struct, DI framework,
or empty folders. Simple endpoints can call a concrete typed query component.

Preserve Python semantics deliberately: missing/null values in PATCH, bool/int
coercion, large integers, decimal JSON, timezone handling, validation error ordering,
query duplicates, cursor encoding, regex behavior, password hashes, token formats,
transactions, cancellation and externally visible error responses. Existing accounts
must continue to authenticate; never regenerate passwords or tokens during conversion.
Untranslatable Python functions, reflection, arbitrary ORM calls and external effects
block completion. This is behavior lowering, not general Python-to-Go transpilation.

## Completion and verification

Maintain separate results for generated, builds, behavior verified, database verified,
and release qualified. Report capabilities per target/profile, not a single flattering
route percentage. An unsupported middleware or model invariant can block the backend
even if all route stubs exist.

A qualified Flask migration must pass all of the following:

- Fresh locked install, syntax/import checks and boot through its application factory.
- Existing Flask fixtures plus the shared FastAPI supported-scope corpus, with explicit
  target-specific contract differences reviewed rather than silently accepted.
- Source/target HTTP comparisons and database/media effects, with successful source
  preconditions so matching failures do not count as successful behavior.
- Actual standalone SQLAlchemy target execution without Django, DRF, FastAPI, Sanka
  runtime, source modules or converter packages installed.
- Empty-schema migration and verified existing-schema adoption tests on SQLite and
  PostgreSQL; no automatic writes to customer databases.
- Authorization isolation, rollback, concurrent duplicate writes, connection cleanup,
  bounded request bodies, configuration errors and graceful serving shutdown.
- Repeated generation across clean directories, process hash seeds and fixture order;
  all generated file hashes identical under the declared locked inputs.
- Regeneration ownership checks: refuse edited-file overwrite and path/symlink escapes.
- No unresolved in-scope gaps, source-framework forwarding or silently excluded routes.
- Public CI and exact-commit private converter regression, followed by artifact/wheel
  installation tests. Publication and deployment are separate approved operations.

Generated tests provide useful application coverage, but do not alone establish
correctness: independent source-derived scenarios and negative fixtures must catch
deliberately broken permissions, rollback and validation behavior.

The current shared replay engine uses isolated SQLite databases. Add PostgreSQL
replay explicitly: separate source/target fixture databases, isolated credentials,
schema and sequence snapshots, cleanup on failure, and a guard rejecting non-test
database targets. SQLite replay must not stand in for PostgreSQL acceptance.

## Ecosystem documentation checked

These inform the proposed choices; they are not evidence that the generators have
implemented them.

- [Flask factories](https://flask.palletsprojects.com/en/stable/patterns/appfactories/): factory and Blueprint composition.
- [SQLAlchemy transaction scopes](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html): explicit commit/rollback ownership.
- [Alembic autogeneration](https://alembic.sqlalchemy.org/en/latest/autogenerate.html): candidate migration review and detection limits.
- [chi](https://github.com/go-chi/chi): net/http-compatible routing.
- [Gin documentation](https://gin-gonic.com/en/docs/): target-specific routing and middleware.
- [Fiber documentation](https://docs.gofiber.io/): current v3 API and request-context lifetime constraints.
- [sqlc with pgx](https://docs.sqlc.dev/en/latest/guides/using-go-and-pgx.html): typed PostgreSQL queries and pool integration.
- [Goose SQL migrations](https://pressly.github.io/goose/documentation/annotations/): sequential migration filenames and transaction controls.
- [GORM documentation](https://gorm.io/docs/index.html): separately qualified optional ORM profile.
