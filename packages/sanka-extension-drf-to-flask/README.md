# DRF to Flask

An Apache-2.0 migration extension using `sanka-extension/v1`. It scans Django's
resolved DRF routes, creates a deterministic reviewed plan, and emits a native
Flask target plus ORM-only Django settings and a machine-readable gap inventory.
It never imports the Sanka runtime or the FastAPI extension.

This alpha converts recognized JSON `APIView` methods with explicit
`AllowAny`, no authentication, no throttling, JSON-only rendering and no source
middleware. Supported handlers use local values, basic builtins, query parameters
and `Response(data, status=..., headers=...)`. It also converts JSON-only request
bodies with the standard strict JSONParser, isolated Django model imports and
`transaction.atomic()` blocks. Source validation and ORM operations stay intact;
malformed JSON, empty bodies and unsupported content types have differential tests.
Model modules with imports outside the small ORM/stdlib allowlist remain gaps.
Other source globals, serializers, viewsets, custom lifecycle hooks, form/multipart
bodies, authentication and implicit HEAD/OPTIONS remain
manual gaps. Unsupported mapped routes return 501 in generated code; unsupported
URL patterns are listed without inventing a route. Readiness counts converted
method/path pairs, not scaffolded placeholders, and is not an accuracy score.

Use the existing CLI marketplace/project lock to select `sanka/drf-to-flask`, then
scan, plan with `--to flask --strategy native --generation minimal`, and apply the
reviewed **core** plan hash. The generated directory is an overlay: run it with
the original application's ORM modules on the Python path and its pinned source
dependencies plus Flask 3.1 installed in the destination environment. Inspect
`migration-gaps.json`, repair only the required gaps, and compare source/candidate
responses, database state and side effects in independent tests. No Django/DRF
request dispatcher is carried into the target. This version does not offer the
FastAPI extension's differential replay command.

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
