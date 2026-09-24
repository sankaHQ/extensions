# Python-to-Go 0.1.0a3 release

`api-converters-v0.1.0a2` is published and immutable. Python-to-Go code
changed after that release; the previous 0.1.0a2 manifest checksum could not
be satisfied by the published asset. Version 0.1.0a3 gives those changes
a new wheel, manifest and release tag. Do not replace the a2 asset.

The a3 bundle keeps TypeScript-to-Rust and HTTP replay at 0.1.0a2, TypeScript
capture at 0.1.0a1, and the pinned SDK wheels. The Rust manifest continues to
reference the published a2 release. The a3 tag carries byte-identical copies
of those unchanged wheels for local qualification and the Go manifest's closure.
It does not publish React Native, Compose, Jev, or a replacement SDK.

## Qualification before publication

The `api-release.yml` pull-request jobs build six wheels, check the two
manifests and scoped catalog, and exercise the pinned `sanka-examples` revision
`e4b9990ccc21ec3d1e775b0955f021f2083fc524`. They use published CLI
0.2.12 so this change can be reviewed before CLI 0.3.0 is released. The Go
job also runs the packaged DRF, Flask and FastAPI fixtures against PostgreSQL
across Fiber, chi, mux and Gin. Local qualification covers captured source
tests and an isolated cross-app row transfer; it is not an application cutover.

```bash
uv sync --frozen --all-packages
uv run python scripts/build_api_release.py
uv run python -m pytest tests/test_api_release.py tests/test_marketplace.py -q
```

`--write-manifests` intentionally updates the Go manifest after source changes.
Review its wheel digest before committing. CI only validates checked-in bytes.

## Publication gate

Wait until Sanka CLI 0.3.0 is published. After this change lands, tag its
reviewed merge commit `api-converters-v0.1.0a3` and dispatch
`api-release.yml` on that tag. The dispatch job requires the tag to be on
`main` and installs published CLI 0.3.0 for both pinned example qualifications
before GitHub publishes any assets. Do not publish from a pull-request build.

The release contains six wheels, two manifests, the scoped catalog and two
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
