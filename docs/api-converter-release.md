# Qualified framework identifier release

The a16 API bundle publishes qualified framework identifiers for Python-to-Go 0.1.0a16.
Known `python-*` sources and `go-*` router choices normalize to retained wire
values before validation and hashing; short aliases remain valid.
Test records actual Go test counts and scenario results. Verify records per-scenario
source/Go statuses, response/database comparisons and endpoint coverage. Safe stage
messages are emitted on stderr for a compatible CLI to display; arbitrary child
logs and response bodies are not streamed. JSON response framing is unchanged.
Scan detects supported DRF, Flask and FastAPI entrypoints, models and source databases
without executing application code. Plan chooses the router, destination database
and endpoints. Ambiguous or dynamic inputs require an explicit override.
TypeScript-to-Rust remains 0.1.0a3. Both use the shared
sanka-code-migration 0.1.0a4 wheel. Existing SDK, HTTP replay and TypeScript capture
wheels remain byte-identical. DRF-to-Flask a15 and DRF-to-FastAPI a21 remain on
the separately published extensions-v0.1.0a36 marketplace release.

Explicit `selected_endpoints` limits generated HTTP routes. Successful apply plus
matching owned file hashes locks existing endpoints during cumulative planning.
Edited generated files block automatic regeneration; unowned files are retained.
Required shared models, authentication and schema remain. Verification is separate
and does not turn a passing subset into an application-wide parity claim.

## Qualification before publication

The `api-release.yml` jobs build seven wheels, check the two manifests and scoped
catalog, and exercise the pinned `sanka-examples` revision
`e4b9990ccc21ec3d1e775b0955f021f2083fc524` with published CLI 0.3.6 on every pull
request, main push and release tag.

Only the publication tag also runs the installed-CLI PostgreSQL corpus: all 17
source profiles on Fiber, plus complete DRF, Flask and FastAPI projects on chi,
mux and Gin (26 cases instead of 68). Each case still checks all five CLI
commands, deterministic plans, rejected unreviewed apply, source preservation,
and HTTP/database parity. This samples source/router combinations at the CLI
boundary; native Go qualification retains all four routers, including async,
browser-header, Alembic, existing-row transfer and original-source-test checks.
Those native tests run in `python-to-golang.yml` on PRs and main rather than
being repeated inside the release job. The installed-CLI corpus is not repeated
on main immediately before the same source is tagged. No application cutover is
qualified by these fixture tests.

```bash
uv sync --frozen --all-packages
uv run python scripts/build_api_release.py
uv run python -m pytest tests/test_api_release.py tests/test_marketplace.py -q
```

`--write-manifests` intentionally updates both API manifests after source changes.
Review its wheel digest before committing. CI only validates checked-in bytes.

## Publication gate

After this change lands, tag its
reviewed merge commit `api-converters-v0.1.0a16` and dispatch
`api-release.yml` on that tag. The dispatch job requires the tag to be on
`main` and installs published CLI 0.3.6 for both pinned example qualifications
before GitHub publishes any assets. Do not publish from a pull-request build.

The release contains seven wheels, two manifests, the scoped catalog and three
acceptance reports. After publication, download the assets into a new
directory and validate their exact hashes:

```bash
uv run python scripts/build_api_release.py --check-only \
  --output-dir release/downloaded-api-converters
```

Then run `scripts/qualify_api_release.py` with `--published-revision` pinned to
the full release commit for both `go` and `rust`. Public readback must pass
before describing the release as consumer-verified. The CLI marketplace's
default revision remains unchanged. Passing these fixtures does not qualify
arbitrary applications or a production data cutover.
