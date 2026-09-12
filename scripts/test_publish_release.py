# SPDX-License-Identifier: Apache-2.0
"""Publication ordering, immutable retries and actual GitHub response boundaries."""

from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts import check_release_artifacts
from scripts.publish_release import (
    COMPATIBILITY_WHEEL,
    SDK_WHEEL,
    STAGES,
    Asset,
    GitHubPublisher,
    PublicationError,
    ReleasePlan,
    build_plan,
    checked_assets,
    publish,
    require_workflow,
)
from scripts.test_update_marketplace_hashes import _release_snapshot
from scripts.update_marketplace_hashes import RELEASE_TAG

REVISION = "a" * 40


@pytest.fixture
def plan(tmp_path: Path) -> ReleasePlan:
    assets = []
    for name, stage in zip(
        (COMPATIBILITY_WHEEL, SDK_WHEEL, "dependency.whl", "sales.whl", "marketplace.json"),
        STAGES,
        strict=True,
    ):
        path = tmp_path / name
        path.write_bytes(name.encode())
        assets.append(
            Asset(name, path, stage, hashlib.sha256(name.encode()).hexdigest(), len(name))
        )
    return ReleasePlan(RELEASE_TAG, REVISION, tuple(assets))


class FakePublisher:
    def __init__(self) -> None:
        self.revision = REVISION
        self.remote: dict[str, Any] | None = None
        self.contents: dict[str, bytes] = {}
        self.events: list[tuple[str, str]] = []
        self.prior_sdks: tuple[dict[str, Any], ...] = ()
        self.fail_after: str | None = None
        self.bad_download: str | None = None

    def tag_revision(self, tag: str) -> str:
        return self.revision

    def published_sdk_assets(self) -> tuple[dict[str, Any], ...]:
        return self.prior_sdks

    def release(self, tag: str) -> dict[str, Any] | None:
        if self.remote is None:
            return None
        result = deepcopy(self.remote)
        result["assets"] = [
            {
                "name": name,
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "state": "uploaded",
            }
            for name, data in self.contents.items()
        ]
        return result

    def create(self, plan: ReleasePlan) -> None:
        assert self.remote is None
        self.events.append(("create", plan.tag))
        self.remote = {
            "id": 123,
            "tag_name": plan.tag,
            "target_commitish": plan.revision,
            "draft": False,
            "prerelease": True,
            "body": plan.marker,
        }

    def upload(self, tag: str, assets: tuple[Asset, ...]) -> None:
        for asset in assets:
            assert asset.name not in self.contents, "Publisher must never overwrite an asset"
            self.events.append(("upload", asset.name))
            self.contents[asset.name] = asset.path.read_bytes()
            if asset.name == self.fail_after:
                self.fail_after = None
                raise PublicationError("Upload outcome was uncertain")

    def download(self, tag: str, asset: Asset, destination: Path) -> None:
        self.events.append(("download", asset.name))
        data = self.contents[asset.name]
        destination.write_bytes(data if asset.name != self.bad_download else b"x" * len(data))

    def sdk_probe(self, downloads: dict[str, Path], directory: Path) -> dict[str, Any]:
        assert set(downloads) == {COMPATIBILITY_WHEEL, SDK_WHEEL}
        self.events.append(("sdk_probe", "passed"))
        return {"passed": True}


def test_sdk_is_publicly_read_back_and_installed_before_later_uploads(plan: ReleasePlan) -> None:
    client = FakePublisher()
    evidence = publish(plan, client, sdk_probe=client.sdk_probe)
    assert evidence["outcome"] == "complete"
    assert evidence["verified_stages"] == list(STAGES)
    assert client.events == [
        ("create", RELEASE_TAG),
        ("upload", COMPATIBILITY_WHEEL),
        ("download", COMPATIBILITY_WHEEL),
        ("upload", SDK_WHEEL),
        ("download", SDK_WHEEL),
        ("sdk_probe", "passed"),
        ("upload", "dependency.whl"),
        ("download", "dependency.whl"),
        ("upload", "sales.whl"),
        ("download", "sales.whl"),
        ("upload", "marketplace.json"),
        ("download", "marketplace.json"),
    ]


@pytest.mark.parametrize("stage", STAGES)
def test_uncertain_partial_upload_resumes_only_missing_assets(
    plan: ReleasePlan, stage: str
) -> None:
    client = FakePublisher()
    client.fail_after = plan.stage(stage)[0].name
    with pytest.raises(PublicationError, match="uncertain"):
        publish(plan, client, sdk_probe=client.sdk_probe)
    prior = set(client.contents)
    prior_events = len(client.events)
    result = publish(plan, client, sdk_probe=client.sdk_probe)
    later = client.events[prior_events:]
    assert result["outcome"] == "complete"
    assert {name for action, name in later if action == "upload"}.isdisjoint(prior)
    assert not any(action == "create" for action, _name in later)
    assert ("sdk_probe", "passed") in later
    completed_events = len(client.events)
    publish(plan, client, sdk_probe=client.sdk_probe)
    assert not any(action in {"create", "upload"} for action, _ in client.events[completed_events:])


@pytest.mark.parametrize("change", ["wrong_tag", "wrong_revision", "remote_tag", "local_hash"])
def test_invalid_identity_or_input_hash_stops_before_publication(
    plan: ReleasePlan, change: str
) -> None:
    client = FakePublisher()
    if change == "wrong_tag":
        plan = ReleasePlan("extensions-v0.1.0a17", plan.revision, plan.assets)
    elif change == "wrong_revision":
        plan = ReleasePlan(plan.tag, "main", plan.assets)
    elif change == "remote_tag":
        client.revision = "b" * 40
    else:
        plan.assets[0].path.write_bytes(b"changed")
    with pytest.raises(PublicationError):
        publish(plan, client, sdk_probe=client.sdk_probe)
    assert client.events == [] and client.remote is None


def test_conflicting_sdk_version_under_another_release_cannot_be_republished(
    plan: ReleasePlan,
) -> None:
    client = FakePublisher()
    client.prior_sdks = (
        {
            "name": SDK_WHEEL,
            "digest": "sha256:" + "0" * 64,
            "size": plan.stage("sdk")[0].size,
            "state": "uploaded",
        },
    )
    with pytest.raises(PublicationError, match="already published with conflicting bytes"):
        publish(plan, client, sdk_probe=client.sdk_probe)
    assert client.events == []


def test_existing_compatibility_sdk_is_reused_with_identical_bytes(plan: ReleasePlan) -> None:
    client = FakePublisher()
    compat = plan.stage("compatibility")[0]
    client.prior_sdks = (
        {
            "name": COMPATIBILITY_WHEEL,
            "digest": "sha256:" + compat.sha256,
            "size": compat.size,
            "state": "uploaded",
        },
    )
    assert publish(plan, client, sdk_probe=client.sdk_probe)["outcome"] == "complete"


@pytest.mark.parametrize("change", ["hash", "unknown", "source", "marker", "order"])
def test_conflicting_partial_release_stops_without_new_uploads(
    plan: ReleasePlan, change: str
) -> None:
    client = FakePublisher()
    client.create(plan)
    assert client.remote is not None
    if change == "hash":
        client.contents[COMPATIBILITY_WHEEL] = plan.assets[0].path.read_bytes()
        client.contents[SDK_WHEEL] = b"conflicting-sdk"
    elif change == "unknown":
        client.contents["unreviewed.whl"] = b"unknown"
    elif change == "source":
        client.remote["target_commitish"] = "main"
    elif change == "marker":
        client.remote["body"] = "another publication"
    else:
        client.contents["sales.whl"] = plan.stage("extensions")[0].path.read_bytes()
    client.events.clear()
    with pytest.raises(PublicationError):
        publish(plan, client, sdk_probe=client.sdk_probe)
    assert client.events == []


def test_sdk_public_bytes_must_match_even_if_api_metadata_claims_the_right_hash(
    plan: ReleasePlan,
) -> None:
    client = FakePublisher()
    client.bad_download = SDK_WHEEL
    with pytest.raises(PublicationError, match="SHA-256 readback failed"):
        publish(plan, client, sdk_probe=client.sdk_probe)
    assert set(client.contents) == {COMPATIBILITY_WHEEL, SDK_WHEEL}
    assert not any(action == "sdk_probe" for action, _ in client.events)


def test_failed_sdk_install_prevents_implementing_extension_publication(plan: ReleasePlan) -> None:
    client = FakePublisher()

    def failed_probe(_downloads: dict[str, Path], _directory: Path) -> dict[str, Any]:
        raise PublicationError("SDK import failed")

    with pytest.raises(PublicationError, match="SDK import failed"):
        publish(plan, client, sdk_probe=failed_probe)
    assert set(client.contents) == {COMPATIBILITY_WHEEL, SDK_WHEEL}


def test_reviewed_release_plan_contains_every_wheel_and_unique_metadata_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, dist = _release_snapshot(tmp_path)
    monkeypatch.setattr(
        check_release_artifacts,
        "LOCKED_DEPENDENCY_HASHES",
        {
            name: hashlib.sha256((dist / name).read_bytes()).hexdigest()
            for name in check_release_artifacts.LOCKED_DEPENDENCY_HASHES
        },
    )
    plan = build_plan(root, dist, tag=RELEASE_TAG, revision=REVISION)
    assert [len(plan.stage(stage)) for stage in STAGES] == [1, 1, 184, 8, 10]
    assert len(plan.assets) == len({a.name for a in plan.assets}) == 204
    assert {a.name for a in plan.stage("catalogs")} == {
        "marketplace.json",
        "flow-marketplace.json",
        *(f"{package}.json" for package in check_release_artifacts.MANIFESTS),
    }
    sdk = dist / SDK_WHEEL
    sdk.write_bytes(b"modified artifact")
    with pytest.raises(PublicationError, match="Invalid release artifacts"):
        build_plan(root, dist, tag=RELEASE_TAG, revision=REVISION)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_http_failure_cannot_be_interpreted_as_a_missing_release(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            f'HTTP/2.0 {status} Error\nContent-Type: application/json\n\n{{"message":"failed"}}',
            "",
        ),
    )
    with pytest.raises(PublicationError, match=f"HTTP {status}"):
        GitHubPublisher().release(RELEASE_TAG)


def test_only_an_explicit_http_404_means_release_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            'HTTP/2.0 404 Not Found\nContent-Type: application/json\n\n{"message":"Not Found"}',
            "",
        ),
    )
    assert GitHubPublisher().release(RELEASE_TAG) is None
    with pytest.raises(PublicationError, match="HTTP 404"):
        GitHubPublisher().tag_revision(RELEASE_TAG)


def test_git_api_annotated_tag_and_paginated_release_assets_match_expected_shape(
    monkeypatch: pytest.MonkeyPatch,
    plan: ReleasePlan,
) -> None:
    paths = []
    rows = [{"name": f"asset-{i}", "state": "uploaded"} for i in range(204)]

    def request(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        path = arguments[-1]
        paths.append(path)
        if "/git/ref/" in path:
            value: Any = {"object": {"type": "tag", "sha": "c" * 40}}
        elif "/git/tags/" in path:
            value = {"object": {"type": "commit", "sha": REVISION}}
        elif "/releases/tags/" in path:
            value = {
                "id": 123,
                "tag_name": RELEASE_TAG,
                "target_commitish": REVISION,
                "draft": False,
                "prerelease": True,
                "body": plan.marker,
                "assets": [],
            }
        else:
            page = int(path.rsplit("=", 1)[1])
            value = rows[(page - 1) * 100 : page * 100]
        return subprocess.CompletedProcess(
            arguments,
            0,
            "HTTP/2.0 200 OK\nContent-Type: application/json\n\n" + json.dumps(value),
            "",
        )

    monkeypatch.setattr(subprocess, "run", request)
    client = GitHubPublisher()
    assert client.tag_revision(RELEASE_TAG) == REVISION
    release = client.release(RELEASE_TAG)
    assert release is not None and release["target_commitish"] == REVISION
    assert release["assets"] == rows
    assert len([path for path in paths if "/assets?" in path]) == 3


def test_release_draft_or_wrong_target_cannot_be_adopted(plan: ReleasePlan) -> None:
    client = FakePublisher()
    client.create(plan)
    release = client.release(plan.tag)
    assert release is not None
    for change in ({"draft": True}, {"target_commitish": "main"}, {"prerelease": False}):
        with pytest.raises(PublicationError, match="Existing release"):
            checked_assets(release | change, plan)


def test_local_or_branch_workflow_cannot_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "GITHUB_ACTIONS",
        "GITHUB_REPOSITORY",
        "GITHUB_REF_TYPE",
        "GITHUB_REF_NAME",
        "GITHUB_SHA",
    ):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(PublicationError, match="canonical GitHub Actions tag run"):
        require_workflow(RELEASE_TAG, REVISION)
