# SPDX-License-Identifier: Apache-2.0
"""Independent native-flow contract: no benchmark fixture or solution imports."""

import json
from pathlib import Path

import pytest
from test_lifecycle import call, project


def test_apply_test_verify_without_model_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    project(tmp_path)
    with (tmp_path / "settings.py").open("a") as handle:
        handle.write(
            'import os\nDATABASES={"default":{"ENGINE":"django.db.backends.sqlite3",'
            '"NAME":os.environ.get("SANKA_TEST_DB", "unused.sqlite3")}}\n'
        )
    (tmp_path / "services.py").write_text(
        'def describe(label, units):\n    return {"label": label.upper(), "units": units}\n'
    )
    (tmp_path / "urls.py").write_text("""from types import SimpleNamespace
from django.urls import path
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework import serializers
from services import describe

class HeaderAuth(BaseAuthentication):
    def authenticate_header(self, request):
        return "Token"
    def authenticate(self, request):
        value = request.headers.get("Authorization", "")
        if value != "Token sample":
            raise AuthenticationFailed("A valid token is required.")
        return SimpleNamespace(username="operator", is_authenticated=True), None

class Input(serializers.Serializer):
    label = serializers.CharField(max_length=12)
    units = serializers.IntegerField(min_value=1, max_value=9)
    note = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    def validate(self, attrs):
        if attrs["label"] == "reserved":
            raise serializers.ValidationError("Reserved label.")
        return attrs

class Base(APIView):
    permission_classes = [AllowAny]
    authentication_classes = [HeaderAuth]
    renderer_classes = [JSONRenderer]
    parser_classes = [JSONParser]

class Labels(Base):
    def get(self, request):
        if request.headers.get("If-None-Match") == '"label-v1"':
            return Response(status=304, headers={"ETag": '"label-v1"'})
        return Response({"owner": request.user.username})
    def post(self, request):
        serializer = Input(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.data["label"] = "reserved"
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        return Response(describe(values["label"], values["units"]), status=201)
urlpatterns = [path("labels/", Labels.as_view())]
""")
    scan = call(tmp_path, "scan")
    assert scan["outcome"] == "success", scan
    plan = call(tmp_path, "plan")["data"]
    assert plan["readiness"] == 1, plan["routes"]
    applied = call(tmp_path, "apply", {"extension_plan_hash": plan["plan_hash"]}, "reviewed")
    assert applied["outcome"] == "success", applied
    tested = call(tmp_path, "test", {"extension_plan_hash": plan["plan_hash"]}, "reviewed")
    assert tested["outcome"] == "success", tested
    headers = {"Authorization": "Token sample"}
    cases = [
        {
            "id": "read",
            "method": "GET",
            "path": "/labels/",
            "headers": headers,
            "expected_source_status": 200,
        }
    ]
    cases.append(
        {
            "id": "conditional",
            "method": "GET",
            "path": "/labels/",
            "headers": {**headers, "If-None-Match": '"label-v1"'},
            "capture_headers": ["Allow", "ETag"],
            "expected_source_status": 304,
        }
    )
    for index, body in enumerate(
        [
            {"label": "bolt", "units": 2},
            {},
            [],
            None,
            {"label": "reserved", "units": 2},
            {"label": False, "units": True},
            {"label": 123, "units": "2.0"},
            {"label": "a" * 13, "units": 10},
            {"label": "\u0000", "units": 1},
            {"label": "  ", "units": "1.1"},
        ]
    ):
        cases.append(
            {
                "id": f"write-{index}",
                "method": "POST",
                "path": "/labels/",
                "headers": headers,
                "body": body,
            }
        )
    for method in ["GET", "HEAD", "OPTIONS", "DELETE", "TRACE"]:
        for authenticated in [True, False]:
            cases.append(
                {
                    "id": f"{method}-{authenticated}",
                    "method": method,
                    "path": "/labels/",
                    "headers": headers if authenticated else {},
                    "capture_headers": ["Allow", "WWW-Authenticate"],
                }
            )
    cases += [
        {
            "id": "unacceptable",
            "method": "GET",
            "path": "/labels/",
            "headers": {"Accept": "text/html"},
            "capture_headers": ["Allow", "WWW-Authenticate"],
        },
        {
            "id": "unknown-format",
            "method": "GET",
            "path": "/labels/?format=xml",
            "headers": headers,
            "capture_headers": ["Allow"],
        },
    ]
    (tmp_path / "cases.json").write_text(json.dumps(cases))
    verified = call(
        tmp_path, "verify", {"scenarios": "cases.json", "candidate": applied["data"]["output"]}
    )
    assert verified["outcome"] == "success", verified
    assert verified["data"]["summary"]["matched"] == len(cases)
    # A damaged generated artifact cannot pass the structural test gate.
    target = Path(applied["data"]["output"]) / "target_app.py"
    target.write_text("this is invalid python!")
    assert (
        call(tmp_path, "test", {"extension_plan_hash": plan["plan_hash"]}, "reviewed")["outcome"]
        == "error"
    )


@pytest.mark.parametrize("change", ["serializer", "auth", "service"])
def test_unsupported_dependencies_remain_gaps(tmp_path: Path, change: str) -> None:
    project(tmp_path)
    # Reuse the public lifecycle fixture but make a global dependency non-convertible.
    source = (tmp_path / "urls.py").read_text()
    if change == "serializer":
        source = source.replace(
            'return Response({"label": label, "total": quantity * 7}',
            "return Response(self.get_serializer().data",
        )
    elif change == "auth":
        source = source.replace(
            "authentication_classes = []",
            'authentication_classes = [__import__("rest_framework.authentication", '
            'fromlist=["SessionAuthentication"]).SessionAuthentication]',
        )
    else:
        (tmp_path / "service.py").write_text(
            "import rest_framework\ndef total(value):\n    return value * 7\n"
        )
        source = "from service import total\n" + source.replace("quantity * 7", "total(quantity)")
    (tmp_path / "urls.py").write_text(source)
    scanned = call(tmp_path, "scan")
    assert scanned["outcome"] == "success", scanned
    get = next(
        row
        for row in scanned["data"]["routes"]
        if row["method"] == "GET" and row["path"].startswith("/quotes/")
    )
    assert get["classification"] == "needs_adaptation"
    assert get["source"] is None and get["reasons"]
