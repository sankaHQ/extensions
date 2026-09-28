# Python-to-Go 0.1.0a8 release

Version 0.1.0a8 lowers bounded FastAPI Alembic `add_column` operations after
`create_table`: nullable columns without defaults and non-null string, Boolean,
or integer columns with static server defaults. Each downgrade must reverse the
additions. PostgreSQL qualification checks an existing row backfilled by Alembic,
the corresponding generated Goose revision, source-to-Go transfer, HTTP parity,
and rollback across Fiber, chi, mux, and Gin. It needs a new wheel, manifest,
and release tag. Do not replace the published a7 asset.

The a8 bundle keeps TypeScript-to-Rust and HTTP replay at 0.1.0a2, TypeScript
capture at 0.1.0a1, and the pinned SDK wheels. The Rust manifest continues to
reference the published a2 release. The a8 tag carries byte-identical copies
of those unchanged wheels for local qualification and the Go manifest's closure.
It does not publish React Native, Compose, Jev, or a replacement SDK.

## Qualification before publication

The `api-release.yml` jobs build six wheels, check the two manifests and scoped
catalog, and exercise the pinned `sanka-examples` revision
`e4b9990ccc21ec3d1e775b0955f021f2083fc524` with published CLI 0.3.2 on every pull
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

`--write-manifests` intentionally updates the Go manifest after source changes.
Review its wheel digest before committing. CI only validates checked-in bytes.

## Publication gate

After this change lands, tag its
reviewed merge commit `api-converters-v0.1.0a8` and dispatch
`api-release.yml` on that tag. The dispatch job requires the tag to be on
`main` and installs published CLI 0.3.0 for both pinned example qualifications
before GitHub publishes any assets. Do not publish from a pull-request build.

The release contains six wheels, two manifests, the scoped catalog and three
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
