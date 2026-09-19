# SPDX-License-Identifier: Apache-2.0
"""Evidence for static discovery, strict review binding and SDK subprocess transport."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from sanka_extension_llm_to_jev import EXTENSION_ID, VERSION
from sanka_extension_llm_to_jev.adapter import handle
from sanka_extension_llm_to_jev.common import digest, object_digest
from sanka_extension_llm_to_jev.decision import validate_spec
from sanka_extension_llm_to_jev.planning import build_plan
from sanka_extension_llm_to_jev.scanner import scan_project

from sanka_extensions.code import ExtensionRequest, decode_response, encode_request

FIXTURE = Path(__file__).parent / "fixtures/positive"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    return root


def spec(root: Path) -> dict:
    return json.loads((root / "jev-decision.json").read_text())


def request(root: Path, command: str = "scan") -> ExtensionRequest:
    return ExtensionRequest(
        "case",
        command,
        str(root),
        str(root.parent / "artifacts"),
        EXTENSION_ID,
        VERSION,
        "a" * 64,
        {},
        {},
        (),
        None,
    )


def test_positive_inventory_and_review_binding(source: Path) -> None:
    inventory = scan_project(source)
    assert inventory["manual_count"] == 0
    (site,) = inventory["call_sites"]
    assert site["id"] == "classifier.py:classify:11"
    assert site["labels"] == ["billing", "technical", "sales", "unknown"]
    assert site["consumer"] == 'return json.loads(response.output_text)["department"]'
    assert validate_spec(spec(source), inventory) == spec(source)
    assert scan_project(source) == inventory


@pytest.mark.parametrize(
    "old,new",
    [
        ("from openai import OpenAI", "from openai import OpenAI as Factory"),
        ("import json", "import json\nfrom helper import *"),
        ("client = OpenAI()", "client = factory()"),
        ("client = OpenAI()", "client = OpenAI(api_key=get_secret())"),
        ("def classify", "async def classify"),
        ("input=text", "input=[text]"),
        ('"enum": ["billing", "technical", "sales", "unknown"]', '"enum": LABELS'),
        ('return json.loads(response.output_text)["department"]', "return response.output_text"),
        ('return "unknown"', 'return "invented"'),
        ("model=", "tools=[], model="),
        ("def classify", "@wrapper\ndef classify"),
        ("client = OpenAI()", "client = OpenAI()\njson = fake"),
        ("client = OpenAI()", "client = OpenAI()\nfrom fake import module as json"),
        ("client = OpenAI()", "client = OpenAI()\ndef Exception(): pass"),
    ],
)
def test_unsupported_is_manual(source: Path, old: str, new: str) -> None:
    file = source / "classifier.py"
    file.write_text(file.read_text().replace(old, new))
    inventory = scan_project(source)
    assert inventory["manual_count"] >= 1
    assert all(site["status"] == "manual" for site in inventory["call_sites"])
    with pytest.raises(ValueError):
        validate_spec(spec(source), inventory)


def test_import_side_effect_never_executed(source: Path) -> None:
    marker = source.parent / "executed"
    (source / "malicious.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    scan_project(source)
    assert not marker.exists()


def test_alias_accounted_even_alongside_supported(source: Path) -> None:
    with (source / "classifier.py").open("a") as file:
        file.write("\nother_call = client.responses.create\n")
    inventory = scan_project(source)
    assert inventory["manual_count"] == 1
    assert len(inventory["call_sites"]) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("unknown_field", "bad"),
        ("decision_id", "support-router"),
        ("review", {"status": "draft", "reviewer": "author"}),
        (
            "confidence",
            {"threshold": float("nan"), "status": "uncalibrated", "owner": "application"},
        ),
    ],
)
def test_strict_schema_rejects_unknown_or_unreviewed(
    source: Path, field: str, value: object
) -> None:
    decision = spec(source)
    decision[field] = value
    with pytest.raises(ValueError):
        validate_spec(decision, scan_project(source))


def test_mismatched_labels_hash_and_fallback_rejected(source: Path) -> None:
    for change in ("hash", "label", "fallback"):
        decision = spec(source)
        if change == "hash":
            decision["source"]["sha256"] = "0" * 64
        elif change == "label":
            decision["question"]["options"][0]["id"] = "new_label"
        else:
            decision["fallback"]["label"] = "billing"
        with pytest.raises(ValueError):
            validate_spec(decision, scan_project(source))


def test_symlink_source_rejected(source: Path) -> None:
    (source / "escape").symlink_to(source.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        scan_project(source)


def test_plan_is_deterministic_and_binds_every_input(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = types.ModuleType("sanka_extension_llm_to_jev.generator")
    module.render_candidate = lambda root, decision, inventory: {"adapter.py": "# deterministic\n"}
    monkeypatch.setitem(sys.modules, module.__name__, module)
    inventory = scan_project(source)
    plan = build_plan(source, spec(source), inventory, "a" * 64)
    assert build_plan(source, spec(source), inventory, "a" * 64) == plan
    without_hash = copy.deepcopy(plan)
    assert without_hash.pop("plan_hash") == object_digest(without_hash)
    assert "candidate/adapter.py" in plan["diff"]
    assert plan["generated_digests"] == {"adapter.py": digest("# deterministic\n")}
    other_spec = spec(source)
    other_spec["question"]["text"] += " Review this."
    assert build_plan(source, other_spec, inventory, "a" * 64)["plan_hash"] != plan["plan_hash"]
    assert build_plan(source, spec(source), inventory, "b" * 64)["plan_hash"] != plan["plan_hash"]


def test_plan_requires_reviewed_spec(source: Path) -> None:
    (source / "jev-decision.json").unlink()
    with pytest.raises(ValueError, match="specification is required"):
        handle(request(source, "plan"))


def test_subprocess_sdk_transport_success_and_errors(source: Path) -> None:
    payload = encode_request(request(source))
    run = subprocess.run(
        [sys.executable, "-m", "sanka_extension_llm_to_jev"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    response = decode_response(json.loads(run.stdout))
    assert response.request_id == "case" and response.data["supported_count"] == 1
    assert response.artifacts and not run.stderr
    payload["configuration"] = {"decision_spec": "../escape"}
    bad = subprocess.run(
        [sys.executable, "-m", "sanka_extension_llm_to_jev"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert bad.returncode == 1
    assert decode_response(json.loads(bad.stdout)).error.code == "JEV_MIGRATION_REJECTED"
    malformed = subprocess.run(
        [sys.executable, "-m", "sanka_extension_llm_to_jev"],
        input="{}",
        text=True,
        capture_output=True,
        check=False,
    )
    assert malformed.returncode == 2 and not malformed.stdout
    assert "invalid" in malformed.stderr


def test_transport_rejects_root_symlink_before_sdk_normalization(source: Path) -> None:
    alias = source.parent / "alias"
    alias.symlink_to(source, target_is_directory=True)
    payload = encode_request(request(source))
    payload["project_root"] = str(alias)
    run = subprocess.run(
        [sys.executable, "-m", "sanka_extension_llm_to_jev"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )
    assert run.returncode == 2 and not run.stdout


@pytest.mark.parametrize(
    "extra",
    [
        "request: object = client.responses.create\ndef other(text): return request(text)\n",
        "(request := client.responses.create)\n",
        "request = getattr(client.responses, 'create')\n",
        "alias = client\ndef other(text): return alias.responses.create(input=text)\n",
        "def factory(): return client\n",
    ],
)
def test_escaping_provider_references_remain_manual(source: Path, extra: str) -> None:
    file = source / "classifier.py"
    file.write_text(file.read_text() + "\n" + extra)
    inventory = scan_project(source)
    assert inventory["manual_count"] >= 1
    assert any("unresolved" in site["id"] for site in inventory["call_sites"])


def test_shared_line_constructor_is_manual(source: Path) -> None:
    file = source / "classifier.py"
    file.write_text(file.read_text().replace("client = OpenAI()", "client = OpenAI(); value = 42"))
    assert scan_project(source)["call_sites"][0]["status"] == "manual"
