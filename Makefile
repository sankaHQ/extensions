.PHONY: check typescript-bundle

# Most tests start Django, Flask or generated servers in subprocesses, so run
# them on a bounded number of pytest-xdist workers (never -n auto).
TEST_WORKERS ?= 4
ifeq ($(strip $(TEST_WORKERS)),)
$(error TEST_WORKERS must be 1, 2, 3, or 4)
endif
ifneq ($(filter $(TEST_WORKERS),1 2 3 4),$(TEST_WORKERS))
$(error TEST_WORKERS must be 1, 2, 3, or 4)
endif
# Extra pytest arguments; CI uses it to run one lane against the installed wheel.
PYTEST_ARGS ?=

# sanka-ts-capture needs the pinned TypeScript compiler bundle, which is not
# committed. The fetch is idempotent and digest-checked; no network when present.
typescript-bundle:
	uv run python scripts/fetch_typescript_bundle.py

check: typescript-bundle
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages scripts
	uv run python scripts/check_boundaries.py
	uv run python scripts/check_terminology.py
	uv run python scripts/check_catalog_docs.py
	uv run python -m pytest -n $(TEST_WORKERS) $(PYTEST_ARGS)
	uv run python -m pytest scripts/test_update_marketplace_hashes.py scripts/test_sdk_candidate.py -q

.PHONY: build-release build-business-flows

build-business-flows:
	uv run python scripts/build_business_flows.py

build-release:
	uv build --wheel --package sanka-extension-sdk --out-dir release/sdk-candidate --clear --no-create-gitignore
	uv run python scripts/check_sdk_candidate.py release/sdk-candidate
	uv run python scripts/build_release.py --output-dir dist
	uv run python scripts/check_release_artifacts.py dist

.PHONY: update-marketplace-hashes

update-marketplace-hashes:
	uv run python scripts/build_release.py --output-dir dist
	uv run python scripts/update_marketplace_hashes.py --dist dist --release-tag extensions-v0.1.0a33
	uv run python scripts/check_release_artifacts.py dist

.PHONY: converter-bench

BENCH_DIR ?= ../bench
converter-bench:
	@test -n "$(CONVERTER_BENCH_OUTPUT)" || { echo 'Set CONVERTER_BENCH_OUTPUT to a private artifact path outside this repository.' >&2; exit 2; }
	uv run python scripts/run_converter_bench.py --bench-dir "$(BENCH_DIR)" --output "$(CONVERTER_BENCH_OUTPUT)"

.PHONY: build-jev-release

build-jev-release:
	uv run python scripts/build_jev_release.py
