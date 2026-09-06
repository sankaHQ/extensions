# SPDX-License-Identifier: Apache-2.0
"""Regression tests for marketplace wheel closure and immutable hashes."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts import build_release
from scripts.build_release import _prepare_output
from scripts.check_release_artifacts import validate_release
from scripts.update_marketplace_hashes import (
    MANIFEST_DEPENDENCIES,
    MANIFEST_WHEELS,
    update_manifests,
)


def _wheel(directory: Path, name: str) -> None:
    (directory / name).write_bytes(name.encode())


def test_hash_updater_records_each_manifest_dependency_closure(tmp_path: Path) -> None:
    for names in MANIFEST_WHEELS.values():
        for name in names:
            _wheel(tmp_path, name)

    manifests = update_manifests(tmp_path, release_tag="extensions-v0.1.0a14")

    assert set(manifests) == set(MANIFEST_WHEELS)
    for package, payload in manifests.items():
        assert [wheel["name"] for wheel in payload["wheels"]] == list(MANIFEST_WHEELS[package])
        assert all(
            wheel["url"].startswith(
                "https://github.com/sankaHQ/extensions/releases/download/extensions-v0.1.0a14/"
            )
            and len(wheel["sha256"]) == 64
            for wheel in payload["wheels"]
        )


def test_connector_manifests_include_locked_third_party_wheel_variants() -> None:
    assert any(name.startswith("pyyaml-") for name in MANIFEST_WHEELS["sanka-connector-markdown"])
    assert any(
        name.startswith("psycopg_binary-") for name in MANIFEST_WHEELS["sanka-connector-postgres"]
    )


def _locked_dependency_closure(package_name: str) -> set[str]:
    packages = {
        package["name"]: package
        for package in tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))["package"]
    }
    requested_extras: dict[str, set[str]] = {package_name: set()}
    pending = [package_name]
    processed: set[tuple[str, tuple[str, ...]]] = set()
    closure: set[str] = set()
    while pending:
        name = pending.pop()
        state = name, tuple(sorted(requested_extras[name]))
        if state in processed:
            continue
        processed.add(state)
        package = packages[name]
        dependencies = list(package.get("dependencies", ()))
        for extra in requested_extras[name]:
            dependencies.extend(package.get("optional-dependencies", {}).get(extra, ()))
        for dependency in dependencies:
            child = dependency["name"]
            assert child in packages
            closure.add(child)
            extras = set(dependency.get("extra", ()))
            first_request = child not in requested_extras
            prior = requested_extras.setdefault(child, set())
            updated = prior | extras
            if first_request or updated != prior:
                requested_extras[child] = updated
                pending.append(child)
    return {name for name in closure if not name.startswith("sanka-")}


def test_manifest_dependency_sets_cover_every_locked_platform_marker() -> None:
    for package, dependencies in MANIFEST_DEPENDENCIES.items():
        assert set(dependencies) == _locked_dependency_closure(package)
    assert any(
        name.startswith("clickhouse_connect-")
        for name in MANIFEST_WHEELS["sanka-connector-clickhouse"]
    )
    assert all(
        any(name.startswith("tzdata-") for name in MANIFEST_WHEELS[package])
        for package in ("sanka-connector-clickhouse", "sanka-connector-postgres")
    )


def test_locked_dependency_wheels_are_loaded_with_immutable_hashes() -> None:
    wheels = build_release.locked_dependency_wheels()

    assert {wheel.distribution for wheel in wheels} == set(build_release.DEPENDENCIES)
    assert len({wheel.name for wheel in wheels}) == len(wheels)
    assert all(wheel.url.startswith("https://files.pythonhosted.org/") for wheel in wheels)
    assert all(len(wheel.sha256) == 64 for wheel in wheels)
    assert all(0 < wheel.size <= build_release.MAX_DEPENDENCY_WHEEL_BYTES for wheel in wheels)


def test_dependency_download_rejects_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = build_release.LockedWheel(
        distribution="example",
        name="example-1.0-py3-none-any.whl",
        url="https://files.pythonhosted.org/example.whl",
        sha256="0" * 64,
        size=7,
    )
    response = io.BytesIO(b"changed")
    response.headers = {"Content-Length": "7"}  # type: ignore[attr-defined]
    monkeypatch.setattr(build_release, "urlopen", lambda _url, timeout: response)

    with pytest.raises(RuntimeError, match="SHA-256"):
        build_release.download_locked_wheel(tmp_path, wheel)

    assert not (tmp_path / wheel.name).exists()


def test_hash_updater_rejects_an_incomplete_or_wrongly_tagged_wheel_set(tmp_path: Path) -> None:
    _wheel(tmp_path, "sanka_connector_sdk-0.1.0a11-py3-none-any.whl")

    with pytest.raises(RuntimeError, match="complete marketplace wheel set"):
        update_manifests(tmp_path, release_tag="extensions-v0.1.0a14")
    with pytest.raises(RuntimeError, match=r"extensions-v0\.1\.0a14"):
        update_manifests(tmp_path, release_tag="extensions-v0.1.0a14")


def test_build_release_cleanup_is_limited_to_known_wheels(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    output = root / "dist"
    output.mkdir(parents=True)
    stale_wheel = output / "sanka_connector_csv-0.1.0a11-py3-none-any.whl"
    stale_wheel.write_bytes(b"stale")
    keep = output / "operator-notes.txt"
    keep.write_text("keep")

    _prepare_output(output, root=root)

    assert not stale_wheel.exists()
    assert keep.read_text() == "keep"
    with pytest.raises(ValueError, match="repository-owned"):
        _prepare_output(tmp_path / "outside", root=root)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = output / "outside-link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="repository-owned"):
        _prepare_output(link, root=root)


def _metadata_wheel(
    directory: Path,
    *,
    name: str,
    version: str,
    filename: str,
    requirements: tuple[str, ...] = (),
    entry_points: str = "",
) -> Path:
    path = directory / filename
    metadata = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    metadata.extend(f"Requires-Dist: {requirement}" for requirement in requirements)
    dist_info = filename.removesuffix("-py3-none-any.whl").replace("-", "_") + ".dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", "\n".join(metadata) + "\n")
        if entry_points:
            archive.writestr(f"{dist_info}/entry_points.txt", entry_points)
    return path


def _set_manifest_hash(root: Path, package: str, wheel: Path) -> None:
    manifest_path = root / "packages" / package / "extension.json"
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["wheels"]:
        if item["name"] == wheel.name:
            item["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


def _release_snapshot(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "snapshot"
    release = root / "dist"
    release.mkdir(parents=True)
    shutil.copy2(Path("marketplace.json"), root / "marketplace.json")
    packages = {
        "sanka-extension-drf-to-flask": (
            "0.1.0a1",
            "sanka_extension_drf_to_flask-0.1.0a1-py3-none-any.whl",
            "[console_scripts]\n"
            "sanka-extension-drf-to-flask = sanka_extension_drf_to_flask.__main__:main\n",
        ),
        "sanka-extension-sdk": ("0.1.0a1", "sanka_extension_sdk-0.1.0a1-py3-none-any.whl", ""),
        "sanka-extension-drf-to-fastapi": (
            "0.1.0a1",
            "sanka_extension_drf_to_fastapi-0.1.0a3-py3-none-any.whl",
            "[console_scripts]\n"
            "sanka-extension-drf-to-fastapi = sanka_extension_drf_to_fastapi.__main__:main\n",
        ),
        "sanka-connector-sdk": ("0.1.0a11", "sanka_connector_sdk-0.1.0a11-py3-none-any.whl", ""),
        "sanka-connector-markdown": (
            "0.1.0a11",
            "sanka_connector_markdown-0.1.0a11-py3-none-any.whl",
            "[sanka.connectors]\nmarkdown = sanka_connector_markdown:CONNECTOR\n",
        ),
        "sanka-connector-csv": (
            "0.1.0a11",
            "sanka_connector_csv-0.1.0a11-py3-none-any.whl",
            "[sanka.connectors]\ncsv = sanka_connector_csv:CONNECTOR\n",
        ),
        "sanka-connector-sqlite": (
            "0.1.0a11",
            "sanka_connector_sqlite-0.1.0a11-py3-none-any.whl",
            "[sanka.connectors]\nsqlite = sanka_connector_sqlite:CONNECTOR\n",
        ),
        "sanka-connector-postgres": (
            "0.1.0a11",
            "sanka_connector_postgres-0.1.0a11-py3-none-any.whl",
            "[sanka.connectors]\npostgres = sanka_connector_postgres:CONNECTOR\n",
        ),
        "sanka-connector-clickhouse": (
            "0.1.0a11",
            "sanka_connector_clickhouse-0.1.0a11-py3-none-any.whl",
            "[sanka.connectors]\nclickhouse = sanka_connector_clickhouse:CONNECTOR\n",
        ),
    }
    for package, (version, filename, entry_points) in packages.items():
        package_path = root / "packages" / package
        package_path.mkdir(parents=True)
        (package_path / "pyproject.toml").write_text(
            f'[project]\nname = "{package}"\nversion = "{version}"\n'
        )
        requirements = (
            ("sanka-extension-sdk==0.1.0a1",)
            if package in {"sanka-extension-drf-to-fastapi", "sanka-extension-drf-to-flask"}
            else ("sanka-connector-sdk==0.1.0a11",)
            if package.startswith("sanka-connector-") and package != "sanka-connector-sdk"
            else ()
        )
        _metadata_wheel(
            release,
            name=package,
            version=version,
            filename=filename,
            requirements=requirements,
            entry_points=entry_points,
        )
    for wheel in build_release.LOCKED_DEPENDENCY_WHEELS:
        _wheel(release, wheel.name)
    for package in MANIFEST_WHEELS:
        source = Path("packages") / package / "extension.json"
        destination = root / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        manifest = json.loads(destination.read_text())
        for item in manifest["wheels"]:
            item["sha256"] = hashlib.sha256((release / item["name"]).read_bytes()).hexdigest()
        destination.write_text(json.dumps(manifest))
    return root, release


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("connector_entry_point", "exact connector entry point"),
        ("dependency", "does not depend on its exact connector SDK"),
        ("hash", "manifest hash does not match release artifact"),
        ("path", "outside the marketplace snapshot"),
    ],
)
def test_release_validator_rejects_invalid_release_boundaries(
    tmp_path: Path, case: str, expected: str
) -> None:
    root, release = _release_snapshot(tmp_path)
    markdown = release / "sanka_connector_markdown-0.1.0a11-py3-none-any.whl"
    if case == "connector_entry_point":
        _metadata_wheel(
            release,
            name="sanka-connector-markdown",
            version="0.1.0a11",
            filename=markdown.name,
            requirements=("sanka-connector-sdk==0.1.0a11",),
            entry_points="[sanka.connectors]\nmarkdown = attacker:CONNECTOR\n",
        )
        _set_manifest_hash(root, "sanka-connector-markdown", markdown)
    elif case == "dependency":
        _metadata_wheel(
            release,
            name="sanka-connector-markdown",
            version="0.1.0a11",
            filename=markdown.name,
            requirements=("requests==2.0",),
            entry_points="[sanka.connectors]\nmarkdown = sanka_connector_markdown:CONNECTOR\n",
        )
        _set_manifest_hash(root, "sanka-connector-markdown", markdown)
    elif case == "hash":
        manifest_path = root / "packages" / "sanka-connector-markdown" / "extension.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["wheels"][1]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
    else:
        catalog_path = root / "marketplace.json"
        catalog = json.loads(catalog_path.read_text())
        catalog["extensions"][0]["manifest"] = "../outside.json"
        catalog_path.write_text(json.dumps(catalog))

    assert any(expected in error for error in validate_release(root, release))
