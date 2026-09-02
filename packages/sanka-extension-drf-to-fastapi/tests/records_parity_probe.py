# SPDX-License-Identifier: Apache-2.0
"""Serve records-fixture scenarios against one side of a native migration."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("source", "native"), required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--database", required=True)
    parser.add_argument("--scenarios", required=True)
    args = parser.parse_args()

    project = Path(args.project).resolve()
    os.environ["BENCH_DB_PATH"] = str(Path(args.database).resolve())
    os.environ["SANKA_TEST_DB"] = str(Path(args.database).resolve())
    os.environ["DJANGO_SETTINGS_MODULE"] = "precision_project.settings"
    sys.path.insert(0, str(project))

    import django  # type: ignore[import-untyped]

    django.setup()
    from django.core.management import call_command  # type: ignore[import-untyped]
    from django.db import connection  # type: ignore[import-untyped]

    call_command("migrate", interactive=False, verbosity=0, run_syncdb=True)
    from records.models import Record  # type: ignore[import-not-found]

    Record.objects.all().delete()
    rows = [
        (1, "Alpha opening", "retail", "10.00", datetime(2026, 1, 1, 0, 0, tzinfo=UTC)),
        (2, "Beta opening", "retail", "10.00", datetime(2026, 1, 1, 0, 0, tzinfo=UTC)),
        (3, "Operations fee", "ops", "2.50", datetime(2026, 1, 1, 1, 0, tzinfo=UTC)),
        (4, "Alpha renewal", "sales", "100.10", datetime(2026, 1, 1, 2, 0, tzinfo=UTC)),
        (5, "Closing balance", "ops", "0.00", datetime(2026, 1, 1, 3, 0, tzinfo=UTC)),
    ]
    Record.objects.bulk_create(
        [
            Record(id=pk, label=label, category=category, amount=Decimal(amount), posted_at=at)
            for pk, label, category, amount, at in rows
        ]
    )

    scenarios: list[dict[str, Any]] = json.loads(args.scenarios)
    if args.mode == "source":
        results = _run_source(scenarios)
    else:
        if not args.output:
            raise SystemExit("--output is required in native mode")
        connection.close()
        results = _run_native(Path(args.output).resolve(), scenarios)

    connection.close()
    payload = {
        "results": results,
        "database": [
            {**row, "amount": str(row["amount"]), "posted_at": row["posted_at"].isoformat()}
            for row in Record.objects.order_by("id").values(
                "id", "label", "category", "amount", "posted_at"
            )
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


def _run_source(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from django.test import Client  # type: ignore[import-untyped]

    client = Client()
    results = []
    for scenario in scenarios:
        response = client.generic(
            str(scenario["method"]),
            str(scenario["path"]),
            data=_raw_body(scenario),
            content_type="application/json",
            headers=dict(scenario.get("headers", {})),
        )
        results.append(
            {
                "status": response.status_code,
                "body": _body(bytes(response.content)),
                "allow": response.headers.get("Allow"),
                "captured": {
                    name: response.headers.get(name) for name in scenario.get("capture", [])
                },
            }
        )
    return results


def _run_native(output: Path, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sys.path.insert(0, str(output))
    from importlib import import_module

    from fastapi.testclient import TestClient

    app = import_module("app").app
    with TestClient(app, follow_redirects=False) as client:
        results = []
        for scenario in scenarios:
            response = client.request(
                str(scenario["method"]),
                str(scenario["path"]),
                content=_raw_body(scenario),
                headers={"content-type": "application/json", **dict(scenario.get("headers", {}))},
            )
            results.append(
                {
                    "status": response.status_code,
                    "body": _body(response.content),
                    "allow": response.headers.get("Allow"),
                    "captured": {
                        name: response.headers.get(name) for name in scenario.get("capture", [])
                    },
                }
            )
        return results


def _raw_body(scenario: dict[str, Any]) -> str:
    if "raw_body" in scenario:
        return str(scenario["raw_body"])
    body = scenario.get("body")
    return json.dumps(body) if body is not None else ""


def _body(content: bytes) -> Any:
    if not content:
        return None
    try:
        return json.loads(content)
    except ValueError:
        return content.decode("utf-8", "replace")


if __name__ == "__main__":
    raise SystemExit(main())
