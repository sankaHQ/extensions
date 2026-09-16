# SPDX-License-Identifier: Apache-2.0
"""SQLAlchemy list queries preserve the qualified DRF listing contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy as sa
from sanka_code_migration.drf.model import FrameworkScan
from sqlalchemy.orm import Session

from sanka_extension_drf_to_flask.database import render_database
from sanka_extension_drf_to_flask.sqlalchemy import qualify_routes, render_sqlalchemy
from sanka_extension_drf_to_flask.sqlalchemy_listing import (
    capture_listing,
    capture_stock_pagination,
    list_records,
)


@pytest.fixture
def records() -> tuple[Session, sa.Table, dict[str, object]]:
    metadata = sa.MetaData()
    table = sa.Table(
        "record",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("label", sa.String, nullable=False),
        sa.Column("category", sa.String, nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("state", sa.String, nullable=False),
    )
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    session = Session(engine)
    session.execute(
        table.insert(),
        [
            {"id": 1, "label": "Alpha opening", "category": "ops", "score": 10, "state": "open"},
            {"id": 2, "label": "Beta", "category": "sales", "score": 10, "state": "closed"},
            {"id": 3, "label": "Alpha ops", "category": "misc", "score": 20, "state": "closed"},
            {"id": 4, "label": "Gamma", "category": "ops", "score": 20, "state": "open"},
            {"id": 5, "label": "alpha close", "category": "sales", "score": 30, "state": "open"},
        ],
    )
    session.commit()
    resource: dict[str, object] = {
        "pk_column": "id",
        "ordering": ["id"],
        "fields": [
            {"name": "id", "column": "id", "kind": "integer"},
            {"name": "label", "column": "label", "kind": "char"},
            {"name": "category", "column": "category", "kind": "char"},
            {"name": "score", "column": "score", "kind": "integer"},
            {"name": "state", "column": "state", "kind": "choice"},
        ],
    }
    try:
        yield session, table, resource
    finally:
        session.close()
        engine.dispose()


def _serialize(row: object) -> dict[str, object]:
    return dict(row)  # type: ignore[arg-type]


def _listing(**extra: object) -> dict[str, object]:
    return {
        "search": {
            "param": "search",
            "fields": [
                {"name": "label", "lookup": "icontains"},
                {"name": "category", "lookup": "icontains"},
            ],
        },
        "ordering": {
            "param": "ordering",
            "fields": ["score", "label"],
            "default": None,
            "rule": "append-pk-follow-last",
            "pk": "id",
        },
        **extra,
    }


def _ids(payload: object) -> list[int]:
    rows = payload["results"] if isinstance(payload, dict) and "results" in payload else payload
    return [row["id"] for row in rows]  # type: ignore[index,union-attr]


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def test_search_ordering_and_repeated_params_use_drf_last_value(
    records: tuple[object, ...],
) -> None:
    session, table, resource = records
    payload, status = list_records(
        session,
        table,
        resource,
        _listing(),
        {"search": ["ignored", "Alpha,ops"], "ordering": ["score", "-score"]},
        "http://test/api/records/?search=ignored&search=Alpha%2Cops&ordering=score&ordering=-score",
        _serialize,
    )

    assert status == 200
    assert _ids(payload) == [3, 1]


def test_declared_filter_is_sql_backed_and_unknown_lookup_fails_closed(
    records: tuple[object, ...],
) -> None:
    session, table, resource = records
    listing = _listing(filters=[{"param": "state", "name": "state", "lookup": "exact"}])

    payload, status = list_records(
        session,
        table,
        resource,
        listing,
        {"state": ["open", "closed"]},
        "http://test/api/records/?state=open&state=closed",
        _serialize,
    )

    assert status == 200
    assert _ids(payload) == [2, 3]
    listing["filters"] = [{"param": "state", "name": "state", "lookup": "custom"}]
    with pytest.raises(ValueError, match="unsupported filter lookup"):
        list_records(session, table, resource, listing, {}, "http://test/api/records/", _serialize)


def test_page_number_count_links_duplicates_and_invalid_pages(records: tuple[object, ...]) -> None:
    session, table, resource = records
    listing = _listing(
        pagination={
            "kind": "page",
            "page_size": 2,
            "page_param": "page",
            "page_size_param": None,
            "max_page_size": None,
            "last_page_strings": ["last"],
            "invalid_page_message": "Invalid page.",
        }
    )
    url = "http://test/api/records/?tag=one&tag=two&page=2"

    payload, status = list_records(
        session, table, resource, listing, {"tag": ["one", "two"], "page": ["2"]}, url, _serialize
    )

    assert status == 200
    assert payload["count"] == 5
    assert _ids(payload) == [3, 4]
    assert _query(payload["next"]) == {"page": ["3"], "tag": ["one", "two"]}
    assert _query(payload["previous"]) == {"tag": ["one", "two"]}
    error, error_status = list_records(
        session,
        table,
        resource,
        listing,
        {"page": ["0"]},
        "http://test/api/records/?page=0",
        _serialize,
    )
    assert (error, error_status) == ({"detail": "Invalid page."}, 404)


def test_limit_offset_uses_defaults_for_invalid_values_and_keeps_count(
    records: tuple[object, ...],
) -> None:
    session, table, resource = records
    listing = _listing(
        pagination={
            "kind": "limit-offset",
            "default_limit": 2,
            "limit_param": "limit",
            "offset_param": "offset",
            "max_limit": 3,
        }
    )

    payload, status = list_records(
        session,
        table,
        resource,
        listing,
        {"limit": ["bad"], "offset": ["-2"]},
        "http://test/api/records/?limit=bad&offset=-2",
        _serialize,
    )

    assert status == 200
    assert payload["count"] == 5
    assert _ids(payload) == [1, 2]
    assert _query(payload["next"]) == {"limit": ["2"], "offset": ["2"]}


def test_cursor_pages_ties_forward_reverse_and_invalid_cursor(records: tuple[object, ...]) -> None:
    session, table, resource = records
    listing = _listing(
        pagination={
            "kind": "cursor",
            "page_size": 2,
            "ordering": ["-score", "-id"],
            "cursor_param": "cursor",
            "page_size_param": None,
            "max_page_size": None,
            "offset_cutoff": 1000,
            "invalid_cursor_message": "Invalid cursor",
        }
    )
    base = "http://test/api/records/?tag=one&tag=two"

    first, status = list_records(session, table, resource, listing, {}, base, _serialize)
    second, _ = list_records(
        session, table, resource, listing, _query(first["next"]), first["next"], _serialize
    )
    back, _ = list_records(
        session,
        table,
        resource,
        listing,
        _query(second["previous"]),
        second["previous"],
        _serialize,
    )

    assert status == 200
    assert _ids(first) == [5, 4]
    assert _ids(second) == [3, 2]
    assert _ids(back) == [5, 4]
    assert _query(first["next"])["tag"] == ["one", "two"]
    error, error_status = list_records(
        session,
        table,
        resource,
        listing,
        {"cursor": ["not-base64!"]},
        "http://test/api/records/?cursor=not-base64!",
        _serialize,
    )
    assert (error, error_status) == ({"detail": "Invalid cursor"}, 404)


def test_stock_page_and_limit_pagination_capture_has_no_django_import_at_module_load() -> None:
    from django.conf import settings

    if not settings.configured:
        settings.configure(REST_FRAMEWORK={})
    from rest_framework.pagination import LimitOffsetPagination, PageNumberPagination

    class Pages(PageNumberPagination):
        page_size = 4
        page_size_query_param = "size"
        max_page_size = 10

    class Limits(LimitOffsetPagination):
        default_limit = 5
        max_limit = 20

    class PageView:
        pagination_class = Pages

    class LimitView:
        pagination_class = Limits

    assert capture_stock_pagination(PageView) == {
        "kind": "page",
        "page_size": 4,
        "page_param": "page",
        "page_size_param": "size",
        "max_page_size": 10,
        "last_page_strings": ["last"],
        "invalid_page_message": "Invalid page.",
    }
    assert capture_stock_pagination(LimitView) == {
        "kind": "limit-offset",
        "default_limit": 5,
        "limit_param": "limit",
        "offset_param": "offset",
        "max_limit": 20,
    }


def test_capture_listing_qualifies_stock_search_ordering_and_page_pagination() -> None:
    from rest_framework import filters, serializers
    from rest_framework.pagination import PageNumberPagination

    class RecordSerializer(serializers.Serializer):
        id = serializers.IntegerField()
        label = serializers.CharField()
        state = serializers.ChoiceField(choices=("open", "closed"))

    class Pages(PageNumberPagination):
        page_size = 2

    class View:
        serializer_class = RecordSerializer
        pagination_class = Pages
        filter_backends = (filters.SearchFilter, filters.OrderingFilter)
        search_fields = ("label", "state")
        ordering_fields = ("label",)
        ordering = ("-label",)

    listing = capture_listing(View, None)

    assert listing["search"] == {
        "param": "search",
        "fields": [
            {"name": "label", "lookup": "icontains"},
            {"name": "state", "lookup": "icontains"},
        ],
    }
    assert listing["ordering"] == {
        "param": "ordering",
        "fields": ["label"],
        "default": ["-label"],
        "rule": "drf",
        "pk": "id",
    }
    assert listing["pagination"]["kind"] == "page"


def test_capture_listing_rejects_custom_pagination_and_unknown_filter_backends() -> None:
    from rest_framework import filters, serializers
    from rest_framework.pagination import PageNumberPagination

    class RecordSerializer(serializers.Serializer):
        id = serializers.IntegerField()
        label = serializers.CharField()

    class CustomPages(PageNumberPagination):
        page_size = 2

        def get_page_size(self, request: object) -> int:
            return 99

    class CustomPageView:
        serializer_class = RecordSerializer
        pagination_class = CustomPages
        filter_backends = ()

    class CustomFilter(filters.BaseFilterBackend):
        def filter_queryset(self, request: object, queryset: object, view: object) -> object:
            return queryset

    class CustomFilterView:
        serializer_class = RecordSerializer
        pagination_class = None
        filter_backends = (CustomFilter,)

    with pytest.raises(ValueError, match="custom pagination"):
        capture_listing(CustomPageView, None)
    with pytest.raises(ValueError, match="filter backend"):
        capture_listing(CustomFilterView, None)


def test_capture_listing_limits_cursor_positions_to_proven_types() -> None:
    from datetime import UTC
    from zoneinfo import ZoneInfo

    from rest_framework import serializers

    class RecordSerializer(serializers.Serializer):
        id = serializers.IntegerField()
        created_at = serializers.DateTimeField(default_timezone=UTC)
        label = serializers.CharField()

    class View:
        serializer_class = RecordSerializer

    listing = {
        "pagination": {
            "kind": "cursor",
            "ordering": ["-created_at", "-id"],
            "page_size": 2,
        }
    }
    assert capture_listing(View, listing) == listing
    RecordSerializer._declared_fields["created_at"] = serializers.DateTimeField(
        default_timezone=ZoneInfo("Asia/Tokyo")
    )
    assert capture_listing(View, listing) == listing
    listing["pagination"]["ordering"] = ["label"]
    with pytest.raises(ValueError, match="cursor ordering field kind"):
        capture_listing(View, listing)


def test_capture_listing_rejects_spoofed_django_filter_backend() -> None:
    from rest_framework import serializers

    DjangoFilterBackend = type(
        "DjangoFilterBackend",
        (),
        {"__module__": "django_filters.rest_framework.backends"},
    )

    class RecordSerializer(serializers.Serializer):
        id = serializers.IntegerField()
        state = serializers.CharField()
        score = serializers.IntegerField()

    class View:
        serializer_class = RecordSerializer
        pagination_class = None
        filter_backends = (DjangoFilterBackend,)
        filterset_fields: ClassVar = {
            "state": ["exact", "in"],
            "score": ["gte", "lte"],
        }

    with pytest.raises(ValueError, match="filter backend requires manual adaptation"):
        capture_listing(View, None)


def test_generated_page_limit_search_ordering_matches_source_without_source_imports(
    tmp_path: Path,
    contract_databases,
) -> None:
    source = tmp_path / "source"
    app = source / "catalog"
    app.mkdir(parents=True)
    (app / "__init__.py").write_text("")
    (source / "settings.py").write_text(
        """
SECRET_KEY = 'fixture-only'
INSTALLED_APPS = ['rest_framework', 'catalog']
import os,json
DATABASES = {'default': json.loads(os.environ['SANKA_SOURCE_TEST_DATABASE'])}
ROOT_URLCONF = 'urls'
MIDDLEWARE = []
ALLOWED_HOSTS = ['testserver']
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'
TIME_ZONE = 'Asia/Tokyo'
REST_FRAMEWORK = {'DEFAULT_AUTHENTICATION_CLASSES': [], 'UNAUTHENTICATED_USER': None,
 'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
 'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser']}
"""
    )
    (app / "models.py").write_text(
        """
from django.db import models
class Product(models.Model):
    label = models.CharField(max_length=40)
    category = models.CharField(max_length=20)
    score = models.IntegerField()
    posted_at = models.DateTimeField()
    class Meta:
        ordering = ['id']
"""
    )
    (app / "views.py").write_text(
        """
from rest_framework import filters, pagination, serializers, viewsets
from .models import Product
class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ('id', 'label', 'category', 'score', 'posted_at')
class Pages(pagination.PageNumberPagination):
    page_size = 2
class Limits(pagination.LimitOffsetPagination):
    default_limit = 2
    max_limit = 3
class Cursor(pagination.CursorPagination):
    page_size = 2
    ordering = ('-posted_at', '-id')
class ListingBase(viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    filter_backends = (filters.SearchFilter, filters.OrderingFilter)
    search_fields = ('label', 'category')
    ordering_fields = ('score', 'label', 'posted_at', 'id')
class PageProducts(ListingBase):
    pagination_class = Pages
class LimitProducts(ListingBase):
    pagination_class = Limits
class CursorProducts(ListingBase):
    pagination_class = Cursor
"""
    )
    (source / "urls.py").write_text(
        """
from rest_framework.routers import DefaultRouter
from catalog.views import CursorProducts, LimitProducts, PageProducts, ListingBase
router = DefaultRouter()
router.register('products', ListingBase, basename='products')
router.register('pages', PageProducts, basename='pages')
router.register('limits', LimitProducts, basename='limits')
router.register('cursors', CursorProducts, basename='cursors')
urlpatterns = router.urls
"""
    )
    scenarios = [
        "/pages/?page=2&tag=one&tag=two",
        "/pages/?search=ignored&search=Alpha%2Cops&ordering=-score",
        "/pages/?page=0",
        "/limits/?limit=2&offset=1&ordering=-score,-id",
        "/limits/?limit=bad&offset=-2",
        "/products/?search=ignored&search=Alpha&ordering=score,id",
        "/pages/?page=1&page=2&ordering=-score&ordering=score,id",
        "/limits/?limit=1&limit=2&offset=0&offset=1&ordering=score,id",
    ]
    capture = r"""
import base64, json, sys
from urllib.parse import parse_qs, urlencode, urlsplit
from sanka_code_migration.drf.scan import scan_django
from sanka_code_migration.drf.models import capture_schema
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides
scan = scan_django(sys.argv[1], settings_module='settings')
from catalog.models import Product
from django.db import connection
from rest_framework.test import APIClient
with connection.schema_editor() as editor:
    editor.create_model(Product)
Product.objects.bulk_create([
    Product(id=1, label='Alpha opening', category='ops', score=10,
            posted_at='2026-01-01T00:00:00Z'),
    Product(id=2, label='Beta', category='sales', score=10, posted_at='2026-01-01T00:00:00Z'),
    Product(id=3, label='Alpha ops', category='misc', score=20, posted_at='2026-01-02T00:00:00Z'),
    Product(id=4, label='Gamma', category='ops', score=20, posted_at='2026-01-02T00:00:00Z'),
    Product(id=5, label='alpha close', category='sales', score=30,
            posted_at='2026-01-03T00:00:00Z'),
])
client = APIClient()
responses = []
paths = json.loads(sys.argv[2])
def request(path):
    response = client.get(path)
    with connection.cursor() as cursor:
        cursor.execute('SELECT id,label,category,score,posted_at FROM catalog_product ORDER BY id')
        database = list(cursor.fetchall())
    responses.append({'status': response.status_code, 'body': json.loads(response.content),
        'headers': {key:response.headers.get(key) for key in ['Allow','Content-Type','Vary']},
        'database': database})
    return responses[-1]
def relative(url):
    parsed = urlsplit(url)
    return parsed.path + ('?' + parsed.query if parsed.query else '')
for path in paths:
    request(path)
first_path = '/cursors/?tag=one&tag=two'
first = request(first_path)
paths.append(first_path)
second_path = relative(first['body']['next'])
second = request(second_path)
paths.append(second_path)
back_path = relative(second['body']['previous'])
back = request(back_path)
paths.append(back_path)
reverse_path = '/cursors/?ordering=posted_at,id'
request(reverse_path)
paths.append(reverse_path)
invalid_path = '/cursors/?cursor=not-base64!'
request(invalid_path)
paths.append(invalid_path)
parsed = urlsplit(first['body']['next'])
params = parse_qs(parsed.query, keep_blank_values=True)
token = params['cursor'][0]
decoded = parse_qs(base64.b64decode(token).decode('ascii'), keep_blank_values=True)
decoded['r'] = ['x']
tampered_query = urlencode(sorted(decoded.items()), doseq=True).encode('ascii')
params['cursor'] = [base64.b64encode(tampered_query).decode('ascii')]
tampered_path = parsed.path + '?' + urlencode(sorted(params.items()), doseq=True)
request(tampered_path)
paths.append(tampered_path)
print(json.dumps({'scan': scan.to_dict(), 'schema': capture_schema([Product]),
                  'overrides': capture_sqlalchemy_overrides(scan),
                  'scenarios': paths, 'responses': responses},default=str))
"""
    source_database, target_url = contract_databases
    env = os.environ | {"SANKA_SOURCE_TEST_DATABASE": json.dumps(source_database)}
    result = subprocess.run(
        [sys.executable, "-c", capture, str(source), json.dumps(scenarios)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    facts = json.loads(result.stdout)
    scenarios = facts["scenarios"]
    assert facts["responses"][0]["body"]["count"] == 5
    assert [row["status"] for row in facts["responses"][:8]] == [
        200,
        200,
        404,
        200,
        200,
        200,
        200,
        200,
    ]
    assert facts["responses"][2]["body"] == {"detail": "Invalid page."}
    assert [row["id"] for row in facts["responses"][5]["body"]] == [1, 3, 5]
    assert [row["id"] for row in facts["responses"][6]["body"]["results"]] == [3, 4]
    assert [row["id"] for row in facts["responses"][7]["body"]["results"]] == [2, 3]
    assert all(row["database"] == facts["responses"][0]["database"] for row in facts["responses"])
    cursor_responses = facts["responses"][-6:]
    assert cursor_responses[0]["body"]["next"] == cursor_responses[2]["body"]["next"]
    assert cursor_responses[0]["body"]["previous"] == cursor_responses[2]["body"]["previous"]
    assert [row["id"] for row in cursor_responses[0]["body"]["results"]] == [5, 4]
    assert [row["id"] for row in cursor_responses[1]["body"]["results"]] == [3, 2]
    assert cursor_responses[0]["body"]["results"] == cursor_responses[2]["body"]["results"]
    assert [row["id"] for row in cursor_responses[3]["body"]["results"]] == [1, 2]
    assert [row["status"] for row in cursor_responses] == [200, 200, 200, 200, 404, 404]
    assert cursor_responses[4]["body"] == {"detail": "Invalid cursor"}
    assert cursor_responses[5] == cursor_responses[4]

    scan = FrameworkScan.from_dict(facts["scan"])
    qualified = qualify_routes(scan, facts["overrides"])
    assert all(route["native"] for route in qualified), json.dumps(
        [route for route in qualified if not route["native"]], indent=2
    )
    files = {
        **render_database(facts["schema"]),
        **render_sqlalchemy(scan, facts["schema"], overrides=facts["overrides"]),
    }
    target = tmp_path / "target"
    for name, content in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    env["SANKA_DATABASE_URL"] = target_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=target,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    (target / "reference.json").write_text(
        json.dumps({"scenarios": scenarios, "responses": facts["responses"]})
    )
    probe = r'''
import importlib.abc, json, os, sys
from pathlib import Path
from sqlalchemy import create_engine, text
engine = create_engine(os.environ['SANKA_DATABASE_URL'])
with engine.begin() as connection:
    connection.execute(text("""INSERT INTO catalog_product
        (id, label, category, score, posted_at) VALUES
        (1, 'Alpha opening', 'ops', 10, '2026-01-01 00:00:00.000000'),
        (2, 'Beta', 'sales', 10, '2026-01-01 00:00:00.000000'),
        (3, 'Alpha ops', 'misc', 20, '2026-01-02 00:00:00.000000'),
        (4, 'Gamma', 'ops', 20, '2026-01-02 00:00:00.000000'),
        (5, 'alpha close', 'sales', 30, '2026-01-03 00:00:00.000000')"""))
class NoSource(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in {'django', 'rest_framework', 'catalog',
                                  'sanka_code_migration', 'sanka_extension_drf_to_flask'}:
            raise ImportError('source dependency forbidden: ' + name)
sys.meta_path.insert(0, NoSource())
from target_app import create_app
from models import TABLES
from sqlalchemy import select
reference = json.loads(Path('reference.json').read_text())
app = create_app({'TESTING': True})
client = app.test_client()
observed = []
for path in reference['scenarios']:
    response = client.get(path, base_url='http://testserver')
    table = TABLES['catalog_product']
    with engine.connect() as connection:
        rows = connection.execute(select(table.c.id,table.c.label,table.c.category,table.c.score,
            table.c.posted_at).order_by(table.c.id)).all()
    observed.append({'status': response.status_code, 'body': response.json,
        'headers': {key:response.headers.get(key) for key in ['Allow','Content-Type','Vary']},
        'database': json.loads(json.dumps([list(row) for row in rows],default=str))})
assert observed == reference['responses'], json.dumps(
    {'observed': observed, 'expected': reference['responses']}, indent=2)
assert not {'django', 'rest_framework', 'catalog'}.intersection(sys.modules)
app.extensions['sanka_engine'].dispose()
engine.dispose()
'''
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=target,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
