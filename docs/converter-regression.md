# Converter regression check

Before releasing a changed DRF-to-FastAPI converter, run **Converter regression**
in the private `sankaHQ/bench` repository for the exact reviewed extensions commit.
Link the successful private workflow URL and its `converter-regression-<SHA>`
artifact from the extensions PR/release review; never upload their contents to
the public repository. A different commit requires a new run.

```bash
gh workflow run converter-regression.yml --repo sankaHQ/bench --ref main \
  -f extensions_sha=<full-40-character-extensions-commit-sha>
```

The workflow checks out that public extensions SHA, verifies it, and reads the
reviewed evaluator/fixture commit from `scripts/converter_bench.json`. Its own
read-only GitHub token accesses the private benchmark. No cross-repository secret
is needed. The workflow rejects a branch name, a short SHA, or an untrusted
workflow ref. Missing checkouts or reports fail the run.

This is a required manual converter release/regression check. It is not automatic
coverage on public extensions PRs. Private fixtures and evaluation reports remain
in private benchmark runs; do not attach their contents to a public PR.

## Local check

Use a clean `sankaHQ/bench` checkout at the manifest's exact benchmark revision.
The runner rejects changed evaluator code, fixtures, dependency metadata, or task
coverage. Prepare both locked environments:

```bash
uv sync --frozen --all-packages
uv sync --project <bench-checkout> --frozen --python 3.12 --extra fixture --group dev
make converter-bench BENCH_DIR=<bench-checkout> \
  CONVERTER_BENCH_OUTPUT=<private-artifact-directory>/converter-regression.json
```

Only run trusted synthetic benchmark fixtures. The runner processes them
sequentially in temporary directories and strips operator credentials from child
environments. Source inspection and evaluation use Bench's same locked Python
3.12/Django/DRF environment. The evaluator is imported from the pinned benchmark source, and the actual
converter and Extension SDK source come from this extensions checkout. Nothing is installed into a customer application;
no network server or customer migration is started.

## What passes

The runner sends `scan`, `plan`, and `apply` through the converter's versioned
stdio contract. It obtains the converter plan hash and returned artifact locations
from protocol responses. It does not read core CLI artifact files or assume a
core plan hash is an extension plan hash.

- Every pinned task must meet its reviewed minimum native-route count and exact
  eligible-route count. New tasks or changed inventories require review.
- Fully generatable candidates must pass every independent evaluator hard gate,
  including native serving, behavior, database effects and determinism.
- Partial candidates must preserve the readiness floor and the independently
  measured metric floors and passing hard gates in `partial_evaluation_floors`.
  These values come from the unchanged converter baseline against the pinned
  evaluator. Missing metrics or changed scenario totals fail. Known failing
  behavior/database checks remain disclosed; `partial` never means a completed
  migration. Numeric floors do not assert that every individual scenario is
  unchanged; detailed private results remain part of release review.
- Below the default 50% threshold, apply must refuse with a gap report and no
  candidate. For nonzero partial coverage, the runner subsequently opts in to
  scaffold evaluation. Zero-route tasks must refuse and remain ungenerated.

The evaluator grades the converter's Bench candidate projection, which retains
Django ORM. This check does not establish standalone generated Tortoise-project
runtime acceptance.

To update benchmark coverage, review and pin its new commit and update the route
and independent evaluation floors together, then execute the real converter/evaluator run. Unit tests of
the runner alone do not satisfy this check.
