# Releasing Sanka extension packages

All six packages share one version and one reviewed source tag. Publishing is
manual; merges and tags do not upload packages automatically.

## Package order

Publish `sanka-connector-sdk` first. Publish the five provider packages only
after the SDK upload succeeds, because every provider declares the SDK as its
only Sanka dependency.

## Local preparation

```bash
uv sync --frozen --all-packages
make check
make build-release
uv run python scripts/check_release_tag.py v0.1.0a10 tag
```

`make build-release` writes per-package wheels and source distributions under
`release/packages/`, a combined set under `release/all/`, the exact source
commit, and SHA-256 hashes. It does not publish anything.

## Trusted publishing setup

Each PyPI and TestPyPI project must trust this exact identity:

- owner: `sankaHQ`
- repository: `sanka-extensions`
- workflow: `publish.yml`
- environment: `pypi` or `testpypi`

Create pending trusted publishers for projects that do not exist yet. Keep the
GitHub environments restricted to version tags. No long-lived PyPI token
belongs in repository secrets or local files.

Keep `SANKA_EXTENSIONS_PUBLISH_ENABLED` and
`SANKA_EXTENSIONS_BOOTSTRAP_ENABLED` absent or `false` outside an explicitly
approved publication window. The workflow refuses to upload without the
matching variable set to `true`.

## Publication gate

1. Merge the exact reviewed commit through `sanka-pr-flow`.
2. Create and push `v<version>` only after authorized-human approval of the
   commit and local artifact hashes.
3. Dispatch **Publish extension packages** against that exact tag, using
   TestPyPI first.
4. Clean-install the SDK and every provider from TestPyPI; verify entry-point
   discovery and provider-specific imports.
5. Obtain explicit approval for the production artifact hashes, then dispatch
   the PyPI target. PyPI versions are immutable.
6. Clean-install from PyPI and verify `sanka-connector-sdk` has zero runtime
   dependencies and each provider installs only its own client/driver stack.

The bootstrap targets publish one package for first-project creation. Bootstrap
the SDK before any provider and use the exact confirmation string shown by the
workflow input.
