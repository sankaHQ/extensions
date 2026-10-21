# SPDX-License-Identifier: Apache-2.0
"""Offline source/target replay with parent-owned disposable database observations."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from sanka_http_replay import compare, load_scenarios, validate_observations

from .capture import canonical, capture, digest, safe_file
from .render import render

RUNNERS = Path(__file__).parent / "runners"
LOCKS = Path(__file__).parent / "locks"
PHP_VERSION = "8.4.27"
LARAVEL_VERSION = "12.70.1"
MAX_BYTES = 20_000_000


def snapshot(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("apply the reviewed plan before replay")
    result: dict[str, bytes] = {}
    size = 0
    for directory, names, files in os.walk(root, followlinks=False):
        if any((Path(directory) / name).is_symlink() for name in names):
            raise ValueError("candidate directory contains a symlink")
        names[:] = [name for name in names if name not in {"__pycache__", ".pytest_cache"}]
        for name in files:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("candidate contains a nonregular file")
            size += path.stat().st_size
            if size > MAX_BYTES or len(result) >= 1000:
                raise ValueError("candidate exceeds bounded replay limits")
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def route_coverage(
    routes: list[dict[str, Any]], scenarios: list[dict[str, Any]]
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {r["method"] + " " + r["path"]: [] for r in routes}
    for scenario in scenarios:
        if "expected_status" not in scenario:
            raise ValueError("every PHP scenario must declare its expected status")
        matches = []
        for route in routes:
            pattern = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "[^/]+", route["path"])
            if route["method"] == scenario["method"] and re.fullmatch(
                pattern, urlsplit(scenario["path"]).path
            ):
                matches.append(route["method"] + " " + route["path"])
        if len(matches) != 1:
            raise ValueError("each scenario must exercise exactly one selected route")
        result[matches[0]].append(scenario["id"])
    if any(not ids for ids in result.values()):
        raise ValueError("selected routes lack replay scenarios")
    return result


def environment(work: Path, database: str | None = None) -> dict[str, str]:
    # Do not forward provider, cloud, production database or shell credentials.
    result = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(work),
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "APP_ENV": "testing",
        "APP_DEBUG": "false",
        "APP_KEY": "",
        "API_TOKEN": "sanka-fixture-token",
        "TZ": "UTC",
    }
    result.update(ALICE_API_TOKEN="sanka-alice-token", BOB_API_TOKEN="sanka-bob-token")
    socket = os.environ.get("SANKA_PHP_MYSQL_SOCKET")
    if socket:
        path = Path(socket)
        if not path.is_absolute() or path != path.resolve() or not path.is_socket():
            raise ValueError("MySQL replay socket must be an absolute live Unix socket")
        result["DATABASE_SOCKET"] = socket
    if database:
        result["DATABASE_URL"] = database
    return result


def run(
    command: list[str], cwd: Path, env: dict[str, str], *, data: Any = None, timeout: int = 600
) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            input=None if data is None else canonical(data),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("qualified replay tool failed or exceeded its time limit") from error
    if result.returncode:
        # Neither subprocess arguments nor the observer's credential-bearing input are reported.
        raise ValueError("qualified replay tool failed; inspect the isolated worker log")
    return result.stdout


def observer(php: str, work: Path, payload: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(
        run([php, str(RUNNERS / "observe.php")], work, environment(work), data=payload, timeout=30)
    )
    if not isinstance(value, dict):
        raise ValueError("invalid database observation")
    return value


@contextlib.contextmanager
def databases(
    php: str, work: Path, captured: dict[str, Any], verify: bool
) -> Iterator[dict[str, str]]:
    dialect = captured["database"]["dialect"]
    if dialect == "none":
        yield {}
        return
    names = ["source", "candidate"] if verify else ["candidate"]
    urls: dict[str, str] = {}
    allocated: list[str] = []
    admin = os.environ.get("SANKA_PHP_MYSQL_ADMIN_URL", "")
    if dialect == "mysql":
        parsed = urlsplit(admin)
        if (
            parsed.scheme != "mysql"
            or parsed.hostname not in {"127.0.0.1", "localhost", "mysql"}
            or parsed.path != "/sanka_php_admin"
            or parsed.query
            or not parsed.username
        ):
            raise ValueError(
                "MySQL replay requires the isolated sanka_php_admin service on loopback or mysql"
            )
    try:
        for side in names:
            if dialect == "sqlite":
                url = "sqlite:///" + str(work / f"{side}.sqlite")
            else:
                name = f"sanka_php_{'s' if side == 'source' else 't'}_{secrets.token_hex(6)}"
                password = secrets.token_urlsafe(24)
                allocated.append(name)
                observer(
                    php,
                    work,
                    {"action": "provision", "url": admin, "name": name, "password": password},
                )
                assert parsed.hostname is not None
                host = parsed.hostname + (f":{parsed.port}" if parsed.port else ":3306")
                url = urlunsplit(
                    ("mysql", f"{name}:{quote(password, safe='')}@{host}", "/" + name, "", "")
                )
            urls[side] = url
            observer(php, work, {"action": "schema", "url": url, "models": captured["models"]})
        yield urls
    finally:
        failures = []
        for name in allocated:
            try:
                observer(php, work, {"action": "drop", "url": admin, "name": name})
            except ValueError:
                failures.append(name)
        if failures:
            raise ValueError("disposable MySQL cleanup failed for: " + ", ".join(failures))


class Probe:
    """One JSON response per request; fixed time and output limits, no TCP server."""

    def __init__(self, command: list[str], work: Path, env: dict[str, str], log: Path) -> None:
        self.log = log.open("wb")
        self.process = subprocess.Popen(
            command,
            cwd=work,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            start_new_session=True,
        )
        self.selector = selectors.DefaultSelector()
        assert self.process.stdout is not None
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.pending = b""
        self.deadline = time.monotonic() + 600

    def exchange(self, scenario: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin is not None and self.process.stdout is not None
        try:
            self.process.stdin.write((canonical(scenario) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise ValueError("replay probe exited before handling a request") from error
        deadline = min(self.deadline, time.monotonic() + 30)
        while b"\n" not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise ValueError("replay request exceeded its time limit")
            chunk = os.read(self.process.stdout.fileno(), 65_536)
            if not chunk:
                raise ValueError("replay probe exited without an observation")
            self.pending += chunk
            if len(self.pending) > 4_194_304:
                raise ValueError("replay observation exceeds the output limit")
        line, self.pending = self.pending.split(b"\n", 1)
        record = json.loads(line)
        if not isinstance(record, dict) or set(record) != {"status", "media_type", "body"}:
            raise ValueError("replay probe returned an invalid response envelope")
        return record

    def close(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.selector.close()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
        self.log.close()


def source_kit(work: Path, captured: dict[str, Any]) -> tuple[str, dict[str, str]]:
    php = shutil.which(os.environ.get("SANKA_PHP_BINARY", "php"))
    if php is None:
        raise ValueError("replay requires the patched PHP source toolchain")
    version = run([php, "-r", "echo PHP_VERSION;"], work, environment(work), timeout=15)
    if version != PHP_VERSION:
        raise ValueError(f"replay requires qualified PHP {PHP_VERSION}")
    identity = {"php": version}
    if captured["kind"] == "laravel":
        root = Path(os.environ.get("SANKA_PHP_FRAMEWORK", ""))
        expected = LOCKS / "laravel" / "composer.lock"
        if (
            not expected.is_file()
            or not root.is_absolute()
            or not (root / "vendor/autoload.php").is_file()
        ):
            raise ValueError("Laravel replay requires the prepared, locked framework runtime")
        if (root / "composer.lock").read_bytes() != expected.read_bytes():
            raise ValueError("Laravel runtime lock differs from the qualified source profile")
        source_lock = captured["source_inventory"].get("composer.lock")
        if source_lock != hashlib.sha256(expected.read_bytes()).hexdigest():
            raise ValueError("Laravel source lock differs from the prepared runtime")
        installed = json.loads((root / "vendor/composer/installed.json").read_text())
        versions = {
            item["name"]: item["version"].removeprefix("v") for item in installed["packages"]
        }
        if versions.get("laravel/framework") != LARAVEL_VERSION:
            raise ValueError("Laravel runtime version differs from the qualified source profile")
        identity.update(laravel=LARAVEL_VERSION, composer_lock=source_lock)
    return php, identity


def target_command(
    target: str, candidate: Path, work: Path, generated: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    if target == "fastapi":
        executable = os.environ.get("SANKA_PHP_PYTHON", sys.executable)
        expected = {
            match.group(1).lower(): match.group(2)
            for match in re.finditer(
                r"(?m)^([A-Za-z0-9_-]+)(?:\[[^\]]+\])?==([^\s\\]+)", generated["requirements.txt"]
            )
        }
        check = (
            "import json,sys,importlib.metadata as m; print(json.dumps({"
            "'python':'.'.join(map(str,sys.version_info[:3])),"
            "'packages':{n:m.version(n) for n in json.loads(sys.argv[1])}}))"
        )
        actual = json.loads(
            run(
                [executable, "-I", "-c", check, canonical(sorted(expected))],
                work,
                environment(work),
            )
        )
        if actual["python"] != "3.12.13" or actual["packages"] != expected:
            raise ValueError("FastAPI replay requires the prepared locked Python environment")
        runner = work / "python-probe.py"
        runner.write_bytes((RUNNERS / "target.py.txt").read_bytes())
        return [executable, "-I", str(runner), str(candidate)], {
            "python": actual["python"],
            "dependencies": digest(expected),
        }
    go = shutil.which("go")
    if go is None:
        raise ValueError("Go replay requires the qualified Go toolchain")
    env = environment(work) | {
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
        "GOMAXPROCS": "2",
    }
    for key in ("GOMODCACHE", "GOCACHE"):
        configured = os.environ.get("SANKA_PHP_" + key)
        if not configured or not Path(configured).is_absolute():
            raise ValueError(f"Go replay requires prepared SANKA_PHP_{key}")
        env[key] = configured
    if run([go, "version"], work, env).split()[2] != "go1.27.0":
        raise ValueError("Go replay requires Go 1.27.0")
    probe = candidate / "cmd/sanka-probe/main.go"
    if probe.exists():
        raise ValueError("candidate uses a reserved replay path")
    probe.parent.mkdir(parents=True)
    probe.write_bytes((RUNNERS / "target.go.txt").read_bytes())
    binary = work / "candidate-probe"
    run(
        [go, "build", "-p", "2", "-mod=readonly", "-o", str(binary), "./cmd/sanka-probe"],
        candidate,
        env,
    )
    return [str(binary)], {"go": "1.27.0", "dependencies": digest(generated["go.sum"])}


def recapture(root: Path, captured: dict[str, Any]) -> dict[str, Any]:
    fresh = capture(root)
    selected = {(r["method"], r["path"]) for r in captured["routes"]}
    fresh["routes"] = [
        route for route in fresh["routes"] if (route["method"], route["path"]) in selected
    ]
    return fresh


def replay(
    root: Path, output: Path, captured: dict[str, Any], target: str, command: str
) -> dict[str, Any]:
    if sys.platform != "linux" or os.environ.get("SANKA_PHP_REPLAY") != "1":
        raise ValueError(
            "PHP replay runs only in an explicitly prepared disposable Linux sandbox "
            "(SANKA_PHP_REPLAY=1)"
        )
    scenarios = load_scenarios(safe_file(root, "sanka-verify.json"))
    coverage = route_coverage(captured["routes"], scenarios)
    original = snapshot(output)
    generated = render(captured, target)
    protected = (
        ("contract.json", "requirements.in", "requirements.txt", ".python-version", "php_compat.py")
        if target == "fastapi"
        else ("go.mod", "go.sum", "runtime.go")
    )
    if any(original.get(name) != generated[name].encode() for name in protected):
        raise ValueError("candidate runtime or dependency locks differ from the reviewed plan")
    if recapture(root, captured) != captured:
        raise ValueError("source changed before replay")
    result: dict[str, Any] = {
        "schema": "sanka.php-replay/v1",
        "command": command,
        "source_digest": captured["source_digest"],
        "candidate_digest": digest(
            {name: hashlib.sha256(value).hexdigest() for name, value in original.items()}
        ),
        "scenario_digest": digest(scenarios),
        "complete_backend": False,
        "discovered_routes": captured["discovered_routes"],
        "selected_routes": len(coverage),
        "route_inventory_complete": captured["route_inventory_complete"],
        "scope": (
            "Declared requests: status, JSON media type/body, "
            "complete declared tables and identity sequences."
        ),
    }
    with tempfile.TemporaryDirectory(prefix="sanka-php-replay-") as directory:
        work = Path(directory)
        php, source_identity = source_kit(work, captured)
        candidate = work / "candidate"
        candidate.mkdir()
        for name, value in original.items():
            path = candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        candidate_command, target_identity = target_command(target, candidate, work, generated)
        source = work / "source"
        source.mkdir()
        for name in captured["source_inventory"]:
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(safe_file(root, name).read_bytes())
        base = work / "laravel-app"
        for name in ("bootstrap/cache", "storage/logs", "config"):
            (base / name).mkdir(parents=True, exist_ok=True)
        settings = work / "source-settings.json"
        settings.write_text(
            canonical(
                {
                    "source": str(source),
                    "base": str(base),
                    "framework": os.environ.get("SANKA_PHP_FRAMEWORK", ""),
                    "routes": [
                        {
                            key: route[key]
                            for key in ("method", "path", "file", "handler")
                            if key in route
                        }
                        for route in captured["routes"]
                    ],
                }
            )
        )
        sides = ["source", "candidate"] if command == "verify" else ["candidate"]
        observed: dict[str, list[dict[str, Any]]] = {}
        with databases(php, work, captured, command == "verify") as urls:
            for side in sides:
                probe_command = (
                    [php, str(RUNNERS / "source.php"), str(settings)]
                    if side == "source"
                    else candidate_command
                )
                probe = Probe(
                    probe_command, work, environment(work, urls.get(side)), work / f"{side}.log"
                )
                try:
                    observations = []
                    for scenario in scenarios:
                        observation = probe.exchange(scenario)
                        observation.update(
                            id=scenario["id"], method=scenario["method"], path=scenario["path"]
                        )
                        if side in urls:
                            observation.update(
                                observer(
                                    php,
                                    work,
                                    {
                                        "action": "snapshot",
                                        "url": urls[side],
                                        "models": captured["models"],
                                    },
                                )
                            )
                        observations.append(observation)
                    observed[side] = validate_observations(observations, scenarios)
                finally:
                    probe.close()
        comparison = compare(scenarios, observed["candidate"], observed.get("source"))
        failed = {step["id"] for step in comparison["steps"] if step["problems"]}
        result.update(
            observed=observed,
            comparison=comparison,
            ok=comparison["ok"],
            source_toolchain=source_identity,
            target_toolchain=target_identity,
            routes=[
                {
                    "id": route,
                    "scenarios": ids,
                    "status": "failed"
                    if set(ids) & failed
                    else ("verified" if command == "verify" else "tested"),
                }
                for route, ids in coverage.items()
            ],
            untested_routes=captured["discovered_routes"] - len(coverage),
        )
    if recapture(root, captured) != captured or snapshot(output) != original:
        raise ValueError("source or candidate changed during replay; discard observations")
    return result
