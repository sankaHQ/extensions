.PHONY: check

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages
	uv run python scripts/check_boundaries.py
	uv run pytest

.PHONY: build-release

build-release:
	uv run python scripts/build_release.py
	uv run python scripts/check_release_artifacts.py
