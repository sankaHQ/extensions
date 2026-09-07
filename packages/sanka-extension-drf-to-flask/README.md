# DRF to Flask

An Apache-2.0 migration extension using `sanka-extension/v1`. It scans Django's
resolved DRF routes, creates a deterministic reviewed plan, and emits a native
Flask target plus ORM-only Django settings and a machine-readable gap inventory.
It never imports the Sanka runtime or the FastAPI extension.

This alpha converts recognized JSON APIView handlers and configuration-only
APIView inheritance. It preserves JSON parsing, isolated Django ORM modules,
transaction blocks, and framework-independent project functions whose transitive
imports stay inside the permitted ORM/stdlib boundary.

The native envelope also includes plain serializers with CharField/IntegerField
validation and self-independent object validation, plus a single self-independent
header authenticator returning a user/token pair or raising AuthenticationFailed.
That authentication subset requires AllowAny and UNAUTHENTICATED_USER=None;
session authentication, multiple authenticators, custom permissions, throttling,
custom dispatch, serializer saves/nested fields, and middleware remain manual gaps.
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
