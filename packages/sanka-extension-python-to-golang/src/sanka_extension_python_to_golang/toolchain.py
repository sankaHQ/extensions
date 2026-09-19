# SPDX-License-Identifier: Apache-2.0
"""Provision the qualified Go compiler without changing the system installation."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

VERSION = "1.26.5"
# Official go.dev release checksums; never resolve a floating release at runtime.
ARCHIVES = {
    ("darwin", "amd64"): (
        "6231d8d3b8f5552ec6cbf6d685bdd5482e1e703214b120e89b3bf0d7bf1ef725",
        67836304,
    ),
    ("darwin", "arm64"): (
        "efb87ff28af9a188d0536ef5d42e63dd52ba8263cd7344a993cc48dd11dedb6a",
        64738542,
    ),
    ("linux", "amd64"): (
        "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053",
        66879095,
    ),
    ("linux", "arm64"): (
        "fe4789e92b1f33358680864bbe8704289e7bb5fc207d80623c308935bd696d49",
        63759990,
    ),
    ("windows", "amd64"): (
        "97e6b2a833b6d89f9ff17d25419ac0a7e3b482a044e9ab18cdef834bd834fd38",
        74927386,
    ),
    ("windows", "arm64"): (
        "f96ee46396d69f1e231c8d981ec6a70216238a646a1f2cd74aea0d0016bbc017",
        71440103,
    ),
}


def _qualified(executable: str) -> bool:
    try:
        result = subprocess.run(
            [executable, "version"],
            capture_output=True,
            text=True,
            timeout=15,
            env=os.environ | {"GOTOOLCHAIN": "local", "GOENV": "off", "GOROOT": ""},
        )
        return result.returncode == 0 and result.stdout.split()[2:3] == [f"go{VERSION}"]
    except (OSError, subprocess.TimeoutExpired):
        return False


def _extract(archive: Path, destination: Path, windows: bool) -> None:
    def safe(name: str) -> None:
        parts = PurePosixPath(name).parts
        if not parts or parts[0] != "go" or ".." in parts or "\\" in name or ":" in name:
            raise ValueError("unsafe Go archive path")

    if windows:
        with zipfile.ZipFile(archive) as zipped:
            for info in zipped.infolist():
                safe(info.filename)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Go archive contains a symlink")
            zipped.extractall(destination)
    else:
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                safe(member.name)
                if not (member.isfile() or member.isdir()):
                    raise ValueError("Go archive contains a special file")
            tar.extractall(destination, filter="data")


def ensure_go(root: Path) -> tuple[str, dict[str, str]]:
    """Reuse a qualified compiler or atomically install a checksummed private copy."""
    cache = root / ".sanka" / "go-toolchain"
    for path in (root / ".sanka", cache):
        if path.is_symlink():
            raise ValueError("Go toolchain cache must not be a symlink")
        path.mkdir(exist_ok=True)
    environment = {"GOROOT": ""}
    for variable, name in (("GOCACHE", "build"), ("GOPATH", "modules")):
        # The CLI strips HOME; direct runs can reuse their existing Go caches.
        if os.environ.get(variable) or os.environ.get("HOME"):
            continue
        path = cache / name
        if path.is_symlink():
            raise ValueError("Go toolchain cache must not be a symlink")
        path.mkdir(exist_ok=True)
        environment[variable] = str(path)
    installed = shutil.which("go")
    if installed and _qualified(installed):
        return installed, environment
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, machine
    )
    if (system, arch) not in ARCHIVES:
        raise ValueError(
            f"Automatic Go installation is unsupported on {system}/{arch}. "
            f"Install Go {VERSION} from https://go.dev/doc/install and add it to PATH."
        )
    checksum, size = ARCHIVES[system, arch]
    destination = cache / f"go{VERSION}.{system}-{arch}"
    binary = Path("go/bin/go.exe" if system == "windows" else "go/bin/go")
    receipt = destination / "archive.sha256"
    if destination.is_symlink():
        raise ValueError("Go toolchain cache must not be a symlink")
    if destination.exists():
        if (
            receipt.is_file()
            and receipt.read_text() == checksum
            and _qualified(str(destination / binary))
        ):
            return str(destination / binary), environment
        raise ValueError(f"Incomplete Go toolchain cache: remove {destination} and retry.")
    suffix = "zip" if system == "windows" else "tar.gz"
    url = f"https://go.dev/dl/go{VERSION}.{system}-{arch}.{suffix}"
    print(f"Installing Go {VERSION} into {destination}", file=sys.stderr, flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix="install-", dir=cache) as temporary:
            staging = Path(temporary)
            archive = staging / "archive"
            digest = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(url, timeout=30) as response, archive.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > size:
                        raise ValueError("Go archive exceeds pinned size")
                    digest.update(chunk)
                    output.write(chunk)
            if total != size or digest.hexdigest() != checksum:
                raise ValueError("Go archive checksum or size mismatch")
            _extract(archive, staging, system == "windows")
            archive.unlink()
            if not _qualified(str(staging / binary)):
                raise ValueError("Downloaded Go compiler failed its version check")
            (staging / "archive.sha256").write_text(checksum)
            try:
                staging.rename(destination)
            except OSError:
                # Another process may have completed the same atomic installation.
                if (
                    not receipt.is_file()
                    or receipt.read_text() != checksum
                    or not _qualified(str(destination / binary))
                ):
                    raise
    except (
        OSError,
        ValueError,
        tarfile.TarError,
        zipfile.BadZipFile,
        urllib.error.URLError,
    ) as error:
        raise ValueError(
            f"Automatic Go {VERSION} installation failed: {error}. "
            f"Retry with network access or install https://go.dev/dl/#go{VERSION} "
            "and add Go to PATH."
        ) from error
    return str(destination / binary), environment
