# SPDX-License-Identifier: Apache-2.0
"""Fail closed on release closure, immutable identity and publication scope drift."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from scripts.build_api_release import PACKAGES, ROOT, build, manifest, validate
from scripts.check_release_artifacts import CATALOG


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    for package in PACKAGES[:2]:
        destination = tmp_path / "packages" / package
        destination.mkdir(parents=True)
        for name in ("extension.json", "extension.template.json"):
            shutil.copyfile(ROOT / "packages" / package / name, destination / name)
    return tmp_path


@pytest.mark.parametrize("package", PACKAGES[:2])
def test_complete_immutable_manifests(package: str) -> None:
    data = manifest(package)
    assert data["commands"] == ["scan", "plan", "apply", "test", "verify"]
    assert data["distribution"]["name"] == package


@pytest.mark.parametrize("mutation", ["closure", "url", "sdk", "contract"])
def test_reject_manifest_drift(snapshot: Path, mutation: str) -> None:
    path = snapshot / "packages" / PACKAGES[1] / "extension.json"
    data = json.loads(path.read_text())
    if mutation == "closure":
        data["wheels"].pop(1)
    elif mutation == "url":
        data["wheels"][0]["url"] = "https://example.com/unreviewed.whl"
    elif mutation == "sdk":
        data["wheels"][-1]["sha256"] = "0" * 64
    else:
        data["targets"].append("unsupported")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        manifest(PACKAGES[1], snapshot)


def test_reject_unrelated_release_assets(tmp_path: Path) -> None:
    (tmp_path / "unrelated.whl").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="unexpected release assets"):
        validate(tmp_path)


def test_refuse_output_outside_release(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="below repository release"):
        build(tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_publication_requires_landed_tag_and_both_consumer_jobs() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/api-release.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert jobs["publish"]["needs"] == ["build", "qualify"]
    assert "workflow_dispatch" in jobs["publish"]["if"]
    assert "github.ref_type == 'tag'" in jobs["publish"]["if"]
    guard = jobs["build"]["steps"][1]["run"]
    assert "api-converters-v0.1.0a5" in guard
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in guard
    assert jobs["qualify"]["strategy"]["matrix"]["target"] == ["go", "rust"]
    cli_step = next(
        step
        for step in jobs["qualify"]["steps"]
        if step.get("name", "").startswith("Verify public CLI")
    )
    assert cli_step["env"]["SANKA_API_RELEASE_CLI_VERSION"] == "0.3.0"
    assert jobs["publish"]["permissions"] == {"contents": "write"}


def test_full_catalog_keeps_existing_packages() -> None:
    assert json.loads((ROOT / "marketplace.json").read_text()) == CATALOG


def test_release_gates_packaged_go_database_cli_corpus() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/api-release.yml").read_text())
    job = workflow["jobs"]["qualify"]
    assert "postgres" in job["services"]
    step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Qualify packaged Go backends through the installed CLI"
    )
    assert step["if"] == "matrix.target == 'go'"
    assert step["env"]["SANKA_GO_CLI_TESTS"] == "1"
    assert step["env"]["SANKA_GO_TESTS"] == "1"
    assert "test_golang_cli.py" in step["run"]
    assert "test_packaged_original_tests_are_opt_in_qualification" in step["run"]
    assert "test_gadget_original_tests_are_opt_in_qualification" in step["run"]
    assert "test_postgres_original_tests_use_disposable_database" in step["run"]
    assert "test_copy_existing_from_django_postgres_schema" in step["run"]
    assert any(
        "go-project-acceptance.json" in str(step.get("with", {}).get("path", ""))
        for step in job["steps"]
    )
