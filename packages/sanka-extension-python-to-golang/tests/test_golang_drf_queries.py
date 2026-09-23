# SPDX-License-Identifier: Apache-2.0
"""Conventional querysets must retain visibility, ordering and pagination."""

import json

import pytest
from sanka_extension_python_to_golang.capture import capture
from test_golang_drf_project import project
from test_golang_drf_project import test_general_project_native_replay as native_replay


def query_project(root):
    config = project(root)
    path = root / "orders/views.py"
    path.write_text(
        path.read_text()
        .replace(
            "from rest_framework.viewsets import ModelViewSet",
            "from rest_framework.viewsets import ModelViewSet\n"
            "from rest_framework.filters import OrderingFilter\n"
            "from rest_framework.pagination import LimitOffsetPagination",
        )
        .replace("Order.objects.all()", "Order.objects.filter(status='new')")
        + "\n    filter_backends = [OrderingFilter]\n"
        + "    ordering_fields = ['id', 'reference']\n"
        + "    ordering = ['-id']\n"
        + "    pagination_class = LimitOffsetPagination\n"
    )
    path = root / "shop_config/settings.py"
    path.write_text(
        path.read_text().replace(
            '"UNAUTHENTICATED_USER": None,', '"UNAUTHENTICATED_USER": None, "PAGE_SIZE": 2,'
        )
    )
    cases = []
    for i, status in enumerate(["new", "paid", "new", "new"]):
        cases.append(
            {
                "id": f"create-{i}",
                "method": "POST",
                "path": "/api/orders/",
                "expected_status": 201,
                "body": {
                    "reference": f"o{i}",
                    "status": status,
                    "items": [{"sku": "a", "quantity": 1, "price": "1.00"}],
                },
            }
        )
    for i, query in enumerate(
        [
            "",
            "?limit=1",
            "?limit=1&offset=1",
            "?limit=1&offset=3",
            "?limit=0&offset=-1",
            "?limit=bad&offset=bad",
            "?limit=1&ordering=id",
            "?ordering=unknown,-id",
            "?ordering=reference&limit=1&offset=1&tag=a&tag=b",
            "?limit=2&limit=1&offset=2",
            "?limit=1&offset=99",
            "?limit=1&offset=1&x=a%20b",
            "?limit=1&offset=18446744073709551617",
            "?limit=0_1&offset=0_1",
            "?limit=%D9%A1&offset=%D9%A1",
            "?limit=1&offset=1&x=%FF&y=%ZZ&z=a;b",
            "?limit=1.0&offset=1e0",
            "?limit=1&ordering=%20-id%20,unknown&offset=1",
        ]
    ):
        cases.append(
            {
                "id": f"list-{i}",
                "method": "GET",
                "path": "/api/orders/" + query,
                "expected_status": 200,
            }
        )
    for method in ("GET", "PATCH", "DELETE"):
        cases.append(
            {
                "id": "hidden-" + method,
                "method": method,
                "path": "/api/orders/2/",
                "expected_status": 404,
                **({"body": {}} if method == "PATCH" else {}),
            }
        )
    (root / "sanka-verify.json").write_text(json.dumps({"scenarios": cases}))
    return config


def test_query_contract(tmp_path):
    result = capture(tmp_path, query_project(tmp_path))
    assert not result["gaps"], result["gaps"]
    query = result["drf_project"]["views"][0]["query"]
    assert query == {
        "filters": {"status": "new"},
        "ordering": ["-id"],
        "ordering_fields": ["id", "reference"],
        "pagination": True,
        "page_size": 2,
    }


@pytest.mark.parametrize("declaration", ["", "    ordering = None\n"])
def test_ordering_filter_defaults_to_model_ordering(tmp_path, declaration):
    config = query_project(tmp_path)
    path = tmp_path / "orders/views.py"
    path.write_text(path.read_text().replace("    ordering = ['-id']\n", declaration))
    result = capture(tmp_path, config)
    assert not result["gaps"], result["gaps"]
    assert result["drf_project"]["views"][0]["query"]["ordering"] == ["id"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("status='new'", "status__contains='new'"),
        ("['id', 'reference']", "['items__sku']"),
        ("[OrderingFilter]", "[UnknownFilter]"),
        ("ordering = ['-id']", "ordering = ['?']"),
        ("LimitOffsetPagination\n", "UnknownPagination\n"),
    ],
)
def test_unsupported_query_contract_blocks(tmp_path, before, after):
    config = query_project(tmp_path)
    path = tmp_path / "orders/views.py"
    path.write_text(path.read_text().replace(before, after))
    assert capture(tmp_path, config)["gaps"]


@pytest.mark.parametrize("target", ["fiber", "chi", "mux", "gin"])
def test_query_native_replay(tmp_path, monkeypatch, target):
    native_replay(tmp_path, monkeypatch, target, query_project)
