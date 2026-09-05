.PHONY: check

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages scripts
	uv run python scripts/check_boundaries.py
	uv run pytest
	uv run python -m pytest scripts/test_update_marketplace_hashes.py -q

.PHONY: build-release

build-release:
	uv run python scripts/build_release.py --output-dir dist
	uv run python scripts/check_release_artifacts.py dist

.PHONY: update-marketplace-hashes

update-marketplace-hashes:
	uv run python scripts/build_release.py --output-dir dist
	uv run python scripts/update_marketplace_hashes.py --dist dist --release-tag extensions-v0.1.0a13
	uv run python scripts/check_release_artifacts.py dist

.PHONY: converter-bench

BENCH_DIR ?= ../bench
converter-bench:
	@test -n "$(CONVERTER_BENCH_OUTPUT)" || { echo 'Set CONVERTER_BENCH_OUTPUT to a private artifact path outside this repository.' >&2; exit 2; }
	uv run python scripts/run_converter_bench.py --bench-dir "$(BENCH_DIR)" --output "$(CONVERTER_BENCH_OUTPUT)"
