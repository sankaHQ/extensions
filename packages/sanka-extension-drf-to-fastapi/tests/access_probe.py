# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E402 -- Django setup must precede fixture imports.
"""Focused request-layer parity probe; no local full migration."""

import asyncio
import json
import os
import shutil
import sys
import types
import uuid
from pathlib import Path

fixture, folder, paths, engine = sys.argv[1:5]
generated = Path(sys.argv[5]) if len(sys.argv) > 5 else None
sys.path[:] = [fixture, *json.loads(paths)]
os.environ["DJANGO_SETTINGS_MODULE"] = "board_config.settings"
os.environ["SANKA_TEST_DB"] = str(Path(folder) / "test.sqlite3")
import django

django.setup()
from django.core.management import call_command

call_command("migrate", run_syncdb=True, verbosity=0)
from bulletins.models import Account, Collection, Entry
from bulletins.permissions import CollectionMembers, EntryMembers, ParentMembers
from bulletins.views import (
    AddMember,
    Collections,
    Entries,
    EntryDetail,
    RemoveMember,
    SearchEntries,
)
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from sanka_extension_drf_to_fastapi.access_contracts import (
    capture_creator_membership,
    capture_member_action,
    capture_membership_permission,
    capture_query,
)
from sanka_extension_drf_to_fastapi.django_fastapi import (
    _allow_headers,
    _field_payload,
    _generic_messages,
    _render_native_app,
)
from sanka_extension_drf_to_fastapi.native_async import _RUNTIME, _render_models, _render_store

assert (
    capture_membership_permission(CollectionMembers, Collection)["relation"]["field"] == "members"
)
assert (
    capture_membership_permission(EntryMembers, Entry)["relation"]["foreign_key"] == "collection_id"
)
assert capture_membership_permission(ParentMembers, Entry)["parent_param"] == "pk"
for view, model in [
    (Collections, Collection),
    (Entries, Entry),
    (EntryDetail, Entry),
    (SearchEntries, Entry),
]:
    assert capture_query(view, model), view
assert capture_creator_membership(Collections, Collection)
assert capture_member_action(AddMember), "add action not recognized"
assert capture_member_action(RemoveMember), "remove action not recognized"
from board_config.urls import urlpatterns

from sanka_extension_drf_to_fastapi.django_fastapi import _walk_patterns

scan = _walk_patterns(urlpatterns, root_path=Path(fixture), middleware=())
assert all(r.native for r in scan.routes), [
    (r.key, r.adaptation_reasons) for r in scan.routes if not r.native
]

from bulletins.unsupported import (
    CustomInputMembers,
    CustomListValidation,
    DifferentDuplicate,
    DifferentPredicate,
    ExtraCreate,
    ExtraUpdate,
    SideEffectQuery,
    StaffMembers,
    ValidatedMembers,
)

from sanka_extension_drf_to_fastapi.access_contracts import (
    capture_member_update,
    capture_parent_create,
)

assert capture_membership_permission(StaffMembers, Collection) is None
assert capture_query(SideEffectQuery, Collection) is None
assert capture_query(DifferentPredicate, Collection) is None
assert capture_creator_membership(ExtraCreate, Collection) is None
assert capture_member_update(ExtraUpdate, Collection) is None
for unsupported in (ValidatedMembers, CustomInputMembers, CustomListValidation):
    assert capture_member_update(unsupported, Collection) is None
assert capture_parent_create(DifferentDuplicate, Entry) is None
from rest_framework.authentication import SessionAuthentication, TokenAuthentication

from sanka_extension_drf_to_fastapi.django_fastapi import _view_auth_support


class SessionFallback(Collections):
    authentication_classes = (TokenAuthentication, SessionAuthentication)


assert _view_auth_support(SessionFallback, Collection) is None

alice = Account.objects.create_user(username="alice")
bob = Account.objects.create_user(username="bob")
admin = Account.objects.create_superuser(username="admin", password="test")
tokens = {u.username: Token.objects.create(user=u).key for u in (alice, bob, admin)}
a = Collection.objects.create(id=uuid.UUID(int=1), name="A")
a.members.add(alice)
b = Collection.objects.create(id=uuid.UUID(int=2), name="B")
b.members.add(bob)
ea = Entry.objects.create(id=uuid.UUID(int=3), name="first", purchased=False, collection=a)
eb = Entry.objects.create(id=uuid.UUID(int=4), name="second", purchased=False, collection=b)
missing = str(uuid.UUID(int=99))
cases = [
    ("alice", "/collections/"),
    ("bob", "/collections/"),
    ("admin", "/collections/"),
    (None, "/collections/"),
    ("alice", f"/collections/{a.pk}/"),
    ("alice", f"/collections/{b.pk}/"),
    ("admin", f"/collections/{b.pk}/"),
    (None, f"/collections/{b.pk}/"),
    (None, f"/collections/{missing}/"),
    ("alice", f"/collections/{missing}/"),
    ("alice", f"/collections/{a.pk}/entries/"),
    ("alice", f"/collections/{b.pk}/entries/"),
    ("admin", f"/collections/{b.pk}/entries/"),
    (None, f"/collections/{missing}/entries/"),
    ("alice", f"/collections/{a.pk}/entries/{ea.pk}/"),
    ("alice", f"/collections/{a.pk}/entries/{eb.pk}/"),
    ("admin", f"/collections/{a.pk}/entries/{eb.pk}/"),
    ("alice", f"/collections/{b.pk}/entries/{eb.pk}/"),
    ("alice", "/search/"),
]
cases = [("get", user, path, None) for user, path in cases]
cases += [
    ("options", "alice", f"/collections/{a.pk}/entries/", None),
    ("options", "alice", f"/collections/{b.pk}/entries/", None),
    ("options", None, f"/collections/{missing}/entries/", None),
    ("options", "alice", f"/collections/{a.pk}/", None),
    ("options", "alice", f"/collections/{b.pk}/", None),
    ("options", None, f"/collections/{b.pk}/", None),
    ("options", "alice", f"/collections/{a.pk}/add/", None),
    ("options", "alice", f"/collections/{b.pk}/add/", None),
    ("delete", "alice", f"/collections/{a.pk}/entries/", None),
    ("delete", "alice", f"/collections/{b.pk}/entries/", None),
    ("delete", None, f"/collections/{missing}/entries/", None),
    ("post", "alice", f"/collections/{a.pk}/entries/", {"name": "first", "purchased": True}),
    ("post", "alice", f"/collections/{b.pk}/entries/", {"name": "blocked", "purchased": False}),
    (
        "post",
        "alice",
        f"/collections/{a.pk}/entries/",
        {"name": "new", "purchased": False, "collection": str(b.pk)},
    ),
    ("post", "alice", "/collections/", {"name": "Created"}),
    ("patch", "alice", f"/collections/{a.pk}/entries/{eb.pk}/", {"name": "wrong-parent"}),
    ("patch", "alice", f"/collections/{b.pk}/entries/{eb.pk}/", {"name": "wrong-user"}),
    ("put", "alice", f"/collections/{a.pk}/add/", {"members": [bob.pk, 99999]}),
    ("put", "alice", f"/collections/{a.pk}/add/", {"members": [True]}),
    ("put", "alice", f"/collections/{a.pk}/add/", {"members": None}),
    ("put", "alice", f"/collections/{a.pk}/add/", {"members": "bad"}),
    ("put", "alice", f"/collections/{a.pk}/add/", {"members": []}),
    ("get", "bob", f"/collections/{a.pk}/", None),
    ("put", "alice", f"/collections/{a.pk}/add/", {"members": [bob.pk, bob.pk]}),
    ("get", "bob", f"/collections/{a.pk}/", None),
    ("put", "bob", f"/collections/{a.pk}/remove/", {"members": [alice.pk]}),
    ("get", "alice", f"/collections/{a.pk}/", None),
    ("put", "alice", f"/collections/{a.pk}/add/", {"members": [alice.pk]}),
    ("put", "bob", f"/collections/{a.pk}/remove/", {"members": [bob.pk]}),
    ("get", "bob", f"/collections/{a.pk}/", None),
    ("get", "admin", f"/collections/{a.pk}/", None),
    ("delete", "bob", f"/collections/{a.pk}/", None),
    ("delete", "admin", f"/collections/{a.pk}/", None),
    ("get", "admin", f"/collections/{a.pk}/", None),
]
from django.db import connections

connections.close_all()
shutil.copyfile(Path(folder) / "test.sqlite3", Path(folder) / "before.sqlite3")
expected = []
for method, user, path, payload in cases:
    client = APIClient()
    if user:
        client.credentials(HTTP_AUTHORIZATION="Token " + tokens[user])
    response = (
        getattr(client, method)(path, data=payload, format="json")
        if payload is not None
        else getattr(client, method)(path)
    )
    expected.append((response.status_code, response.json() if response.content else None))


def database_state():
    return {
        "collections": list(Collection.objects.order_by("name").values_list("name", flat=True)),
        "members": sorted(
            (c.name, tuple(c.members.order_by("username").values_list("username", flat=True)))
            for c in Collection.objects.all()
        ),
        "entries": sorted(Entry.objects.values_list("name", "purchased", "collection__name")),
    }


expected_state = database_state()
connections.close_all()
shutil.copyfile(Path(folder) / "before.sqlite3", Path(folder) / "test.sqlite3")
from dataclasses import asdict

from httpx import ASGITransport, AsyncClient

folder = Path(folder)
resources = []
paths = []
for view_ir in scan.view_details.values():
    routes = [
        r for r in scan.routes if r.view == view_ir.name and r.method not in ("HEAD", "OPTIONS")
    ]
    if not routes:
        continue
    ir = scan.serializer_details[routes[0].serializer]
    payload = asdict(view_ir.auth)
    payload["messages"] = dict(view_ir.auth.messages)
    from sanka_extension_drf_to_fastapi.django_fastapi import _create_payload

    spec = {
        "view": view_ir.name,
        "db_table": ir.db_table,
        "pk_attname": ir.pk_attname,
        "lookup": "pk",
        "lookup_url_kwarg": view_ir.lookup_url_kwarg,
        "object_name": ir.object_name,
        "model_class": ir.model_class,
        "fields": [_field_payload(f) for f in ir.fields],
        "auth": payload,
        "access": view_ir.access,
        "create": _create_payload(ir),
        "storage": list(ir.storage),
        "routes": [{"path": r.path, "operation": r.operation} for r in routes],
    }
    resources.append(spec)
    for r in routes:
        paths.append((r.path, r.method, r.operation, spec))
manifest = {
    "resources": resources,
    "allow": _allow_headers(scan.routes),
    "generic_messages": dict(_generic_messages()),
    "options": {r.path: dict(r.options) for r in scan.routes if r.options},
    "routes": [
        {
            "path": r.path,
            "method": r.method,
            "operation": r.operation,
            "source_view": r.view,
            "strategy": "native-crud",
        }
        for r in scan.routes
    ],
}
(folder / "sanka-manifest.json").write_text(json.dumps(manifest))
os.environ["SANKA_DATABASE_URL"] = (
    "sqlite://" if engine == "tortoise" else "sqlite+aiosqlite:///"
) + str(folder / "test.sqlite3")
if generated:
    import importlib

    sys.path.insert(0, str(generated))
    app = importlib.import_module("app").app
    store = importlib.import_module("sanka_store")
else:
    module = types.ModuleType("models")
    exec(_render_models(engine, manifest), module.__dict__)
    sys.modules["models"] = module
    store = types.ModuleType("sanka_store")
    store.__file__ = str(folder / "sanka_store.py")
    exec(_render_store(engine), store.__dict__)
    sys.modules["sanka_store"] = store
    runtime = types.ModuleType("sanka_native")
    runtime.__file__ = str(folder / "sanka_native.py")
    exec(_RUNTIME, runtime.__dict__)
    sys.modules["sanka_native"] = runtime
    rendered_app = {}
    exec(_render_native_app(manifest), rendered_app)
    app = rendered_app["app"]


async def check():
    await store.init_db()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for (method, user, path, payload), result in zip(cases, expected, strict=True):
                response = await client.request(
                    method,
                    path,
                    json=payload,
                    headers={"Authorization": "Token " + tokens[user]} if user else {},
                )
                actual = response.json() if response.content else None
                if response.status_code == 201 and result[0] == 201:
                    assert uuid.UUID(actual["id"]) not in (a.pk, b.pk, ea.pk, eb.pk)
                    actual["id"] = result[1]["id"]
                assert (response.status_code, actual) == result, (
                    method,
                    user,
                    path,
                    result,
                    response.text,
                )
    finally:
        await store.close_db()


asyncio.run(check())
assert database_state() == expected_state, (database_state(), expected_state)
print("Membership, superuser, anonymous lookup order, and cross-parent request parity passed")
