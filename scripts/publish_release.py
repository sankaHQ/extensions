# SPDX-License-Identifier: Apache-2.0
"""Publish one reviewed GitHub wheel release in verified dependency order.

The default command only validates and prints a plan. Publishing is permitted
only in the canonical tag workflow. Retries read back existing assets and add
missing bytes; this module never replaces an asset, tag or release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.build_release import MARKETPLACE_WHEELS  # noqa: E402
from scripts.check_release_artifacts import MANIFESTS, validate_release  # noqa: E402
from scripts.update_marketplace_hashes import RELEASE_TAG  # noqa: E402

REPOSITORY = "sankaHQ/extensions"
COMPATIBILITY_WHEEL = "sanka_connector_sdk-0.1.0a12-py3-none-any.whl"
SDK_WHEEL = "sanka_extension_sdk-0.1.0a3-py3-none-any.whl"
STAGES = ("compatibility", "sdk", "dependencies", "extensions", "catalogs")


class PublicationError(RuntimeError):
    pass


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


@dataclass(frozen=True)
class Asset:
    name: str
    path: Path
    stage: str
    sha256: str
    size: int

    def to_dict(self) -> dict[str, str | int]:
        return {"name": self.name, "stage": self.stage, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class ReleasePlan:
    tag: str
    revision: str
    assets: tuple[Asset, ...]

    @property
    def digest(self) -> str:
        value = {
            "tag": self.tag,
            "revision": self.revision,
            "assets": [a.to_dict() for a in self.assets],
        }
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )

    @property
    def marker(self) -> str:
        return (
            f"<!-- sanka-marketplace-release/v1 revision={self.revision} "
            f"inventory={self.digest} -->"
        )

    def stage(self, name: str) -> tuple[Asset, ...]:
        return tuple(asset for asset in self.assets if asset.stage == name)


def validate_identity(tag: str, revision: str) -> None:
    if tag != RELEASE_TAG or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise PublicationError(
            "Publication requires the exact release tag and full reviewed commit"
        )


def build_plan(root: Path, dist: Path, *, tag: str, revision: str) -> ReleasePlan:
    validate_identity(tag, revision)
    if errors := validate_release(root, dist):
        raise PublicationError("Invalid release artifacts: " + "; ".join(errors))
    implementing = {
        f"{package.replace('-', '_')}-{manifest['distribution']['version']}-py3-none-any.whl"
        for package, manifest in MANIFESTS.items()
    }
    files: list[tuple[str, Path, str]] = []
    for name in MARKETPLACE_WHEELS:
        stage = (
            "compatibility"
            if name == COMPATIBILITY_WHEEL
            else "sdk"
            if name == SDK_WHEEL
            else "extensions"
            if name in implementing
            else "dependencies"
        )
        files.append((name, dist / name, stage))
    files.extend(
        (name, root / name, "catalogs") for name in ("marketplace.json", "flow-marketplace.json")
    )
    files.extend(
        (f"{package}.json", root / "packages" / package / "extension.json", "catalogs")
        for package in MANIFESTS
    )
    assets = []
    for name, path, stage in files:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise PublicationError(f"Release input must be one regular file: {name}")
        assets.append(Asset(name, path, stage, _hash(path), status.st_size))
    if len({asset.name for asset in assets}) != len(assets):
        raise PublicationError("Release asset filenames must be unique")
    return ReleasePlan(
        tag, revision, tuple(sorted(assets, key=lambda a: (STAGES.index(a.stage), a.name)))
    )


class Publisher(Protocol):
    def tag_revision(self, tag: str) -> str: ...
    def published_sdk_assets(self) -> tuple[dict[str, Any], ...]: ...
    def release(self, tag: str) -> dict[str, Any] | None: ...
    def create(self, plan: ReleasePlan) -> None: ...
    def upload(self, tag: str, assets: tuple[Asset, ...]) -> None: ...
    def download(self, tag: str, asset: Asset, destination: Path) -> None: ...


def _command(arguments: list[str], *, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, env=environment, check=False)
    if result.returncode:
        raise PublicationError(
            f"{arguments[0]} operation failed with exit status {result.returncode}"
        )
    return result.stdout


class GitHubPublisher:
    def _api(self, endpoint: str, *, absent_ok: bool = False) -> Any:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "--include", endpoint],
            capture_output=True,
            text=True,
            check=False,
        )
        header, separator, body = result.stdout.partition("\n\n")
        status = re.match(r"HTTP/[^ ]+ (\d{3})", header)
        if not separator or status is None:
            raise PublicationError("GitHub API response has no verifiable HTTP status")
        code = int(status.group(1))
        if absent_ok and code == 404:
            return None
        if result.returncode or not 200 <= code < 300:
            raise PublicationError(f"GitHub API operation failed with HTTP {code}")
        return json.loads(body)

    def tag_revision(self, tag: str) -> str:
        value = self._api(f"repos/{REPOSITORY}/git/ref/tags/{tag}")["object"]
        for _ in range(4):
            if value.get("type") == "commit":
                return cast(str, value["sha"])
            if (
                value.get("type") != "tag"
                or re.fullmatch(r"[0-9a-f]{40}", value.get("sha", "")) is None
            ):
                break
            value = self._api(f"repos/{REPOSITORY}/git/tags/{value['sha']}")["object"]
        raise PublicationError("Release tag does not resolve to a commit")

    def published_sdk_assets(self) -> tuple[dict[str, Any], ...]:
        found = []
        for page in range(1, 21):
            releases = self._api(f"repos/{REPOSITORY}/releases?per_page=20&page={page}")
            if not isinstance(releases, list):
                raise PublicationError("Published SDK release inventory is invalid")
            for release in releases:
                if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
                    raise PublicationError("Published SDK asset inventory is invalid")
                for asset in release["assets"]:
                    if isinstance(asset, dict) and asset.get("name") in {
                        COMPATIBILITY_WHEEL,
                        SDK_WHEEL,
                    }:
                        found.append(asset)
            if len(releases) < 20:
                return tuple(found)
        raise PublicationError("Published SDK inventory exceeds its bounded page limit")

    def release(self, tag: str) -> dict[str, Any] | None:
        value = self._api(f"repos/{REPOSITORY}/releases/tags/{tag}", absent_ok=True)
        if value is None:
            return None
        if not isinstance(value, dict) or type(value.get("id")) is not int:
            raise PublicationError("GitHub release identity is invalid")
        assets = []
        for page in range(1, 11):
            rows = self._api(
                f"repos/{REPOSITORY}/releases/{value['id']}/assets?per_page=100&page={page}"
            )
            if not isinstance(rows, list):
                raise PublicationError("GitHub asset listing is invalid")
            assets.extend(rows)
            if len(rows) < 100:
                value["assets"] = assets
                return value
        raise PublicationError("GitHub asset inventory exceeds its bounded page limit")

    def create(self, plan: ReleasePlan) -> None:
        with tempfile.TemporaryDirectory(prefix="sanka-release-notes-") as temporary:
            notes = Path(temporary) / "notes.md"
            notes.write_text(
                f"{plan.marker}\n\nReviewed source: `{plan.revision}`.\n\n"
                "Assets are published in verified dependency order: compatibility SDK, "
                "unified SDK, dependencies, implementing extensions, then catalogs. The release "
                "is complete only after the matching publication workflow succeeds and all "
                "advertised hashes match.\n"
            )
            _command(
                [
                    "gh",
                    "release",
                    "create",
                    plan.tag,
                    "--repo",
                    REPOSITORY,
                    "--verify-tag",
                    "--target",
                    plan.revision,
                    "--prerelease",
                    "--title",
                    plan.tag,
                    "--notes-file",
                    str(notes),
                ]
            )

    def upload(self, tag: str, assets: tuple[Asset, ...]) -> None:
        _command(
            ["gh", "release", "upload", tag, *[str(a.path) for a in assets], "--repo", REPOSITORY]
        )

    def download(self, tag: str, asset: Asset, destination: Path) -> None:
        url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{asset.name}"
        size = 0
        with urlopen(url, timeout=30) as response, destination.open("xb") as handle:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > asset.size:
                    raise PublicationError(f"Public asset exceeds the reviewed size: {asset.name}")
                handle.write(chunk)


def checked_assets(release: dict[str, Any], plan: ReleasePlan) -> set[str]:
    if (
        release.get("tag_name") != plan.tag
        or release.get("target_commitish") != plan.revision
        or release.get("draft") is not False
        or release.get("prerelease") is not True
        or plan.marker not in str(release.get("body", ""))
    ):
        raise PublicationError(
            "Existing release does not identify this reviewed source and inventory"
        )
    expected = {asset.name: asset for asset in plan.assets}
    found: set[str] = set()
    rows = release.get("assets")
    if not isinstance(rows, list):
        raise PublicationError("Release asset inventory is invalid")
    for row in rows:
        if not isinstance(row, dict) or row.get("name") not in expected or row["name"] in found:
            raise PublicationError("Release has an unknown or duplicate asset")
        asset = expected[row["name"]]
        if (
            row.get("digest") != f"sha256:{asset.sha256}"
            or row.get("size") != asset.size
            or row.get("state") != "uploaded"
        ):
            raise PublicationError(f"Published asset conflicts with reviewed bytes: {asset.name}")
        found.add(asset.name)
    prior: set[str] = set()
    for stage in STAGES:
        names = {asset.name for asset in plan.stage(stage)}
        if found & names and not prior <= found:
            raise PublicationError("Existing assets violate dependency publication order")
        prior.update(names)
    return found


SDK_PROBE = """import importlib.metadata as metadata, json
from sanka_extensions import flow, data, code
from sanka_connector import SourceConnector
assert data.DataReader is SourceConnector
assert metadata.version('sanka-connector-sdk') == '0.1.0a12'
assert metadata.version('sanka-extension-sdk') == '0.1.0a3'
assert metadata.requires('sanka-connector-sdk') in (None, [])
requirements = [r.replace(' ', '').lower() for r in metadata.requires('sanka-extension-sdk')]
assert requirements == ['sanka-connector-sdk==0.1.0a12']
request = flow.BlueprintRequest(
    'published-sdk-probe', flow.ArtifactIdentity('sanka/probe', '1', 'sha256:' + '0' * 64),
    flow.create(type='crm'), flow.TargetIdentity('isolated-probe', '1'), (), {},
)
flow.FlowCapability('crm', (), ()).validate_request(request)
assert flow.BlueprintRequest.from_dict(flow.decode_message(flow.encode_message(request))) == request
assert code.ExtensionRequest is not None
print(json.dumps({
    'compatibility_sdk': '0.1.0a12', 'extension_sdk': '0.1.0a3',
    'flow_protocol': flow.PROTOCOL_VERSION,
}))
"""


def verify_sdk_install(downloads: dict[str, Path], directory: Path) -> dict[str, Any]:
    if sys.version_info[:2] != (3, 12):
        raise PublicationError("Published SDK verification requires Python 3.12")
    environment = {
        "PATH": os.defpath,
        "HOME": str(directory),
        "TMPDIR": str(directory),
        "PYTHONUTF8": "1",
    }
    uv = shutil.which("uv")
    if uv is None:
        raise PublicationError("The canonical uv tool is required for isolated SDK verification")
    venv = directory / "sdk-venv"
    _command(
        [uv, "--no-config", "--offline", "venv", "--python", sys.executable, str(venv)],
        environment=environment,
    )
    requirements = directory / "sdk-requirements.txt"
    requirements.write_text(
        "".join(
            f"{downloads[name].as_uri()} --hash=sha256:{_hash(downloads[name])}\n"
            for name in (COMPATIBILITY_WHEEL, SDK_WHEEL)
        )
    )
    python = venv / "bin" / "python"
    _command(
        [
            uv,
            "--no-config",
            "--no-cache",
            "--offline",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-index",
            "--no-deps",
            "--require-hashes",
            "--requirements",
            str(requirements),
        ],
        environment=environment,
    )
    result = _command([str(python), "-I", "-B", "-c", SDK_PROBE], environment=environment)
    return cast(dict[str, Any], json.loads(result))


def publish(
    plan: ReleasePlan,
    client: Publisher,
    *,
    sdk_probe: Callable[[dict[str, Path], Path], dict[str, Any]] = verify_sdk_install,
    progress: Callable[[dict[str, Any]], None] = lambda _value: None,
) -> dict[str, Any]:
    validate_identity(plan.tag, plan.revision)
    evidence: dict[str, Any] = {
        "outcome": "in_progress",
        "tag": plan.tag,
        "revision": plan.revision,
        "inventory_digest": plan.digest,
        "verified_stages": [],
    }
    with tempfile.TemporaryDirectory(prefix="sanka-release-") as temporary:
        directory = Path(temporary)
        frozen = directory / "frozen"
        frozen.mkdir()
        copies = []
        for asset in plan.assets:
            target = frozen / asset.name
            shutil.copyfile(asset.path, target)
            if target.stat().st_size != asset.size or _hash(target) != asset.sha256:
                raise PublicationError(f"Local release input changed: {asset.name}")
            copies.append(Asset(asset.name, target, asset.stage, asset.sha256, asset.size))
        plan = ReleasePlan(plan.tag, plan.revision, tuple(copies))
        if client.tag_revision(plan.tag) != plan.revision:
            raise PublicationError("Remote tag differs from the reviewed commit")
        sdk_assets = {
            asset.name: asset for asset in plan.assets if asset.stage in {"compatibility", "sdk"}
        }
        for row in client.published_sdk_assets():
            asset = sdk_assets[row["name"]]
            if (
                row.get("digest") != f"sha256:{asset.sha256}"
                or row.get("size") != asset.size
                or row.get("state") != "uploaded"
            ):
                raise PublicationError(
                    f"SDK version was already published with conflicting bytes: {asset.name}"
                )
        remote = client.release(plan.tag)
        if remote is None:
            client.create(plan)
            remote = client.release(plan.tag)
        if remote is None:
            raise PublicationError("Release creation did not produce a readable release")
        release_id = remote.get("id")
        checked_assets(remote, plan)
        downloaded: dict[str, Path] = {}
        for stage in STAGES:
            if client.tag_revision(plan.tag) != plan.revision:
                raise PublicationError("Remote tag changed during publication")
            remote = client.release(plan.tag)
            if remote is None or remote.get("id") != release_id:
                raise PublicationError("Release identity changed during publication")
            found = checked_assets(remote, plan)
            missing = tuple(asset for asset in plan.stage(stage) if asset.name not in found)
            if missing:
                client.upload(plan.tag, missing)
            remote = client.release(plan.tag)
            if remote is None or remote.get("id") != release_id:
                raise PublicationError("Release identity changed after upload")
            found = checked_assets(remote, plan)
            if not {a.name for a in plan.stage(stage)} <= found:
                raise PublicationError(f"Publication stage is incomplete: {stage}")
            for asset in plan.stage(stage):
                target = directory / asset.name
                client.download(plan.tag, asset, target)
                if target.stat().st_size != asset.size or _hash(target) != asset.sha256:
                    raise PublicationError(f"Public asset SHA-256 readback failed: {asset.name}")
                downloaded[asset.name] = target
            if stage == "sdk":
                evidence["sdk_install"] = sdk_probe(downloaded, directory)
            evidence["verified_stages"].append(stage)
            progress(evidence)
        if client.tag_revision(plan.tag) != plan.revision:
            raise PublicationError("Remote tag changed after publication")
        remote = client.release(plan.tag)
        if (
            remote is None
            or remote.get("id") != release_id
            or checked_assets(remote, plan) != {a.name for a in plan.assets}
        ):
            raise PublicationError("Final public release inventory is incomplete")
        evidence.update(outcome="complete", assets=[asset.to_dict() for asset in plan.assets])
        progress(evidence)
        return evidence


def require_workflow(tag: str, revision: str, root: Path = ROOT) -> None:
    validate_identity(tag, revision)
    required = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF_TYPE": "tag",
        "GITHUB_REF_NAME": tag,
        "GITHUB_SHA": revision,
    }
    if any(os.environ.get(key) != value for key, value in required.items()):
        raise PublicationError("Publishing is restricted to the canonical GitHub Actions tag run")
    if _command(["git", "-C", str(root), "rev-parse", "HEAD"]).strip() != revision:
        raise PublicationError("Checked-out source differs from the selected commit")
    if _command(["git", "-C", str(root), "status", "--porcelain"]).strip():
        raise PublicationError("Publication source must be clean")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=RELEASE_TAG)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    try:
        plan = build_plan(ROOT, args.dist, tag=args.tag, revision=args.revision)
        if not args.publish:
            print(
                json.dumps(
                    {
                        "tag": plan.tag,
                        "revision": plan.revision,
                        "inventory_digest": plan.digest,
                        "assets": [a.to_dict() for a in plan.assets],
                    },
                    indent=2,
                )
            )
            return 0
        require_workflow(plan.tag, plan.revision)
        evidence_path = ROOT / "release" / "publication-evidence.json"
        evidence_path.parent.mkdir(exist_ok=True)

        def report(value: dict[str, Any]) -> None:
            evidence_path.write_text(json.dumps(value, indent=2) + "\n")
            print(
                json.dumps(
                    {key: value[key] for key in ("outcome", "tag", "revision", "verified_stages")}
                )
            )

        publish(plan, GitHubPublisher(), progress=report)
    except (PublicationError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Publication stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
