# SPDX-License-Identifier: Apache-2.0
"""Offline Go build and bounded loopback replay of a candidate HTTP server."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import http.client
import http.cookiejar
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._scripts import _REQUEST_SCRIPT
from .replay import ReplayError


def snapshot(root: Path) -> dict[str, str]:
    files = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(
            part.startswith(".") or part in {"vendor", "node_modules", "__pycache__"}
            for part in relative.parts
        ):
            continue
        if path.is_symlink():
            raise ReplayError("Go candidate must not contain symlinks")
        if path.is_file():
            files[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def prepare_candidate(root: Path, entrypoint: str, temp: Path) -> tuple[Path, dict[str, str]]:
    entry = Path(entrypoint)
    if entry.is_absolute() or ".." in entry.parts or entry.suffix != ".go":
        raise ReplayError("Go entrypoint must be a relative .go file inside the candidate")
    files = snapshot(root)
    if entrypoint not in files or "go.mod" not in files:
        raise ReplayError(
            "Go candidate requires go.mod and the selected entrypoint", category="candidate_failure"
        )
    copy = temp / "go-candidate"
    copy.mkdir()
    for name in files:
        path = copy / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((root / name).read_bytes())
    if snapshot(copy) != files:
        raise ReplayError("candidate changed while being copied")
    try:
        result = subprocess.run(
            [
                "go",
                "build",
                "-mod=readonly",
                "-o",
                str(copy / "replay-server"),
                "./" + str(entry.parent),
            ],
            cwd=copy,
            env=os.environ
            | {
                "GOPROXY": "off",
                "GOSUMDB": "off",
                "GOTOOLCHAIN": "local",
                "GOWORK": "off",
                "GOFLAGS": "-buildvcs=false",
            },
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReplayError(f"Go build could not complete: {error}") from error
    if result.returncode:
        raise ReplayError(
            "Go build failed: " + result.stderr[-4000:],
            category="infrastructure_failure"
            if "GOPROXY=off" in result.stderr or "GOTOOLCHAIN=local" in result.stderr
            else "candidate_failure",
        )
    return copy, files


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Seatbelt can deny signalling a dead group; reap its leader before checking.
        if sys.platform != "darwin" or process.poll() is None:
            raise
        try:
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            query = library.proc_listpgrppids
            query.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            query.restype = ctypes.c_int
            member = ctypes.c_int()
            ctypes.set_errno(0)
            count = query(process.pid, ctypes.byref(member), ctypes.sizeof(member))
            gone = count == 0 and ctypes.get_errno() == 0
        except (OSError, AttributeError):
            gone = False
        if not gone:
            raise


def run_candidate(
    root: Path,
    request: Mapping[str, Any],
    setup: list[dict[str, Any]],
    database: str,
    db_env: str,
    media: Path,
) -> dict[str, Any]:
    # Use the identical request encoder as the Django/Python candidate probes.
    encoder: dict[str, Any] = {"payload": {"boundary": "SankaBenchBoundary"}}
    exec(_REQUEST_SCRIPT, encoder)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DATABASE_URL", "SANKA_DATABASE_URL", "DJANGO_SETTINGS_MODULE"}
    }
    environment.update({"PORT": str(port), db_env: database, "BENCH_MEDIA_ROOT": str(media)})
    if db_env == "DATABASE_URL":
        environment[db_env] = Path(database).as_uri()
    with (root / "server.log").open("wb") as log:
        process = subprocess.Popen(
            [str(root / "replay-server")],
            cwd=root,
            env=environment,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )

        def cancelled(signum: int, frame: Any) -> None:
            raise ReplayError("Go replay was interrupted")

        previous_handler = None
        if threading.current_thread() is threading.main_thread():
            previous_handler = signal.signal(signal.SIGTERM, cancelled)
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            _kill_group(process)

        watchdog = threading.Timer(300, expire)
        watchdog.start()
        try:
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    raise ReplayError(
                        "Go server exited before accepting requests", category="candidate_failure"
                    )
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ReplayError("Go server did not become ready") from None
                    time.sleep(0.02)
            result: dict[str, Any] = {}
            cookies = http.cookiejar.CookieJar()
            for step_number, step in enumerate([*setup, request], start=1):
                body, headers = encoder["request_bytes"](step)
                headers.setdefault("host", "testserver")
                cookie_request = urllib.request.Request(
                    "http://testserver" + step["path"], headers=headers
                )
                cookies.add_cookie_header(cookie_request)
                headers = dict(cookie_request.header_items())
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                phase = "request send"
                try:
                    connection.request(step["method"], step["path"], body, headers)
                    phase = "response headers"
                    response = connection.getresponse()
                    cookies.extract_cookies(response, cookie_request)
                    phase = "response body"
                    content = response.read(8 * 1024 * 1024 + 1)
                    if len(content) > 8 * 1024 * 1024:
                        raise ReplayError("Go response exceeds the 8 MiB replay limit")
                    if expired.is_set():
                        raise ReplayError("Go scenario exceeded the 300 second replay limit")
                    result = {
                        "status": response.status,
                        "body_b64": base64.b64encode(content).decode(),
                        "headers": {k.lower(): v for k, v in response.getheaders()},
                        "native": {"compiled_go": True},
                    }
                except TimeoutError as error:
                    # A sent loopback request that stalls is repairable candidate behavior.
                    # Send/startup and watchdog failures still stop infrastructure recovery.
                    if phase != "request send" and not expired.is_set():
                        raise ReplayError(
                            f"Go candidate timed out waiting for {phase} "
                            f"at replay step {step_number} (10 second request limit); "
                            "inspect the handler for blocking work or database connection waits",
                            category="candidate_failure",
                        ) from error
                    raise
                except http.client.RemoteDisconnected as error:
                    status = process.poll()
                    if (
                        phase != "request send"
                        and not expired.is_set()
                        and (status is None or status >= 0)
                    ):
                        raise ReplayError(
                            f"Go candidate closed the response at replay step {step_number}; "
                            "inspect the handler for a crash",
                            category="candidate_failure",
                        ) from error
                    raise
                finally:
                    connection.close()
            return result
        except (OSError, http.client.HTTPException) as error:
            raise ReplayError(f"Go server request failed: {error}") from error
        finally:
            watchdog.cancel()
            if previous_handler is not None:
                signal.signal(signal.SIGTERM, previous_handler)
            # Kill descendants too; a timed-out candidate must not survive the replay.
            _kill_group(process)
            process.wait()
