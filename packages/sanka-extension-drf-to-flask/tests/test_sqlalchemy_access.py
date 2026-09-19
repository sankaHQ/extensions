# SPDX-License-Identifier: Apache-2.0
"""Synchronous SQLAlchemy execution of normalized membership access facts."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sanka_code_migration.drf.model import FrameworkScan
from sqlalchemy.orm import Session

from sanka_extension_drf_to_flask.database import render_database
from sanka_extension_drf_to_flask.planning import _service_operations
from sanka_extension_drf_to_flask.sqlalchemy import qualify_routes, render_sqlalchemy
from sanka_extension_drf_to_flask.sqlalchemy_access import (
    add_creator_membership,
    member_action,
    permission_error,
    prepare_parent_create,
    qualify_access,
    scope_statement,
    serialize_field,
)
from sanka_extension_drf_to_flask.sqlalchemy_listing import list_records


def _database() -> tuple[Session, dict[str, Any], dict[str, Any]]:
    metadata = sa.MetaData()
    accounts = sa.Table(
        "accounts",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("is_superuser", sa.Boolean, nullable=False),
    )
    collections = sa.Table(
        "collections",
        metadata,
        sa.Column("id", sa.Uuid(native_uuid=False), primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("last_activity", sa.DateTime, nullable=False),
    )
    members = sa.Table(
        "collection_members",
        metadata,
        sa.Column("collection_id", sa.Uuid(native_uuid=False), nullable=False),
        sa.Column("account_id", sa.Integer, nullable=False),
        sa.UniqueConstraint("collection_id", "account_id"),
    )
    entries = sa.Table(
        "entries",
        metadata,
        sa.Column("id", sa.Uuid(native_uuid=False), primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("purchased", sa.Boolean, nullable=False),
        sa.Column("collection_id", sa.Uuid(native_uuid=False), nullable=False),
    )
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    session = Session(engine)
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    session.execute(
        sa.insert(accounts),
        [
            {"id": 1, "username": "alice", "is_active": True, "is_superuser": False},
            {"id": 2, "username": "bob", "is_active": True, "is_superuser": False},
            {"id": 3, "username": "admin", "is_active": True, "is_superuser": True},
        ],
    )
    session.execute(
        sa.insert(collections),
        [
            {"id": a, "name": "A", "last_activity": dt.datetime(2026, 1, 1)},
            {"id": b, "name": "B", "last_activity": dt.datetime(2026, 1, 2)},
        ],
    )
    session.execute(
        sa.insert(members),
        [{"collection_id": a, "account_id": 1}, {"collection_id": b, "account_id": 2}],
    )
    session.execute(
        sa.insert(entries),
        [
            {"id": uuid.UUID(int=3), "name": "first", "purchased": False, "collection_id": a},
            {"id": uuid.UUID(int=4), "name": "done", "purchased": True, "collection_id": a},
            {"id": uuid.UUID(int=5), "name": "second", "purchased": False, "collection_id": b},
        ],
    )
    session.commit()
    tables = {
        "accounts": accounts,
        "collections": collections,
        "collection_members": members,
        "entries": entries,
    }
    return session, tables, {"a": a, "b": b}


def _relation(*, foreign_key: str | None = None) -> dict[str, Any]:
    return {
        "db_table": "collections",
        "pk": "id",
        "pk_kind": "uuid",
        "object_name": "Collection",
        "foreign_key": foreign_key,
        "field": "members",
        "table": "collection_members",
        "parent_column": "collection_id",
        "user_column": "account_id",
    }


AUTH = {
    "user": {"table": "accounts", "pk": "id", "superuser": "is_superuser"},
    "messages": {
        "no_credentials": "Authentication credentials were not provided.",
        "forbidden": "You do not have permission to perform this action.",
    },
}


def test_membership_scope_and_permission_preserve_parent_lookup_order() -> None:
    session, tables, keys = _database()
    collection_access = {
        "query": {
            "version": 1,
            "filters": [{"kind": "member", "relation": _relation()}],
            "ordering": ["-last_activity"],
        }
    }
    statement = scope_statement(
        sa.select(tables["collections"]),
        tables["collections"],
        tables,
        collection_access,
        {},
        1,
    )
    assert [row.name for row in session.execute(statement)] == ["A"]
    payload, status = list_records(
        session,
        tables["collections"],
        {
            "fields": [
                {"name": "id", "column": "id", "kind": "uuid"},
                {"name": "name", "column": "name", "kind": "char"},
            ],
            "pk_column": "id",
            "ordering": [],
        },
        {},
        {},
        "http://test/collections/",
        lambda row: {"name": row["name"]},
        base_statement=statement,
    )
    assert (payload, status) == ([{"name": "A"}], 200)

    entry_access = {
        "query": {
            "version": 1,
            "filters": [
                {
                    "kind": "equal",
                    "field": "collection_id",
                    "value": {"kind": "kwarg", "name": "pk"},
                }
            ],
            "ordering": ["purchased"],
        }
    }
    statement = scope_statement(
        sa.select(tables["entries"]),
        tables["entries"],
        tables,
        entry_access,
        {"pk": str(keys["a"])},
        1,
    )
    assert [row.name for row in session.execute(statement)] == ["first", "done"]

    parent_permission = {
        "permission": {
            "kind": "membership",
            "relation": _relation(),
            "parent_param": "pk",
            "superuser": True,
        }
    }
    alice = (
        session.execute(sa.select(tables["accounts"]).where(tables["accounts"].c.id == 1))
        .mappings()
        .one()
    )
    bob = (
        session.execute(sa.select(tables["accounts"]).where(tables["accounts"].c.id == 2))
        .mappings()
        .one()
    )
    admin = (
        session.execute(sa.select(tables["accounts"]).where(tables["accounts"].c.id == 3))
        .mappings()
        .one()
    )
    assert (
        permission_error(session, tables, parent_permission, None, {"pk": keys["a"]}, alice, AUTH)
        is None
    )
    assert (
        permission_error(session, tables, parent_permission, None, {"pk": keys["a"]}, admin, AUTH)
        is None
    )
    assert permission_error(
        session, tables, parent_permission, None, {"pk": keys["a"]}, bob, AUTH
    ) == (
        {"detail": AUTH["messages"]["forbidden"]},
        403,
    )
    assert permission_error(
        session, tables, parent_permission, None, {"pk": keys["a"]}, None, AUTH
    ) == (
        {"detail": AUTH["messages"]["no_credentials"]},
        401,
    )
    assert permission_error(
        session, tables, parent_permission, None, {"pk": uuid.UUID(int=99)}, None, AUTH
    ) == (
        {"detail": "No Collection matches the given query."},
        404,
    )


def test_unknown_access_semantics_fail_qualification() -> None:
    qualify_access(
        {
            "query": {
                "version": 1,
                "filters": [{"kind": "member", "relation": _relation()}],
                "ordering": [],
            }
        },
        {"fields": [], "create_contract": None},
    )
    try:
        qualify_access({"query": {"version": 2, "filters": []}}, {"fields": []})
    except ValueError as error:
        assert "version" in str(error)
    else:
        raise AssertionError("unknown access query version qualified")


def test_membership_and_related_preview_serialization() -> None:
    session, tables, keys = _database()
    member_field = {
        "kind": "membership_read",
        "relation": {
            **_relation(),
            "user": {"table": "accounts", "pk": "id"},
            "output_fields": [
                {"name": "id", "column": "id"},
                {"name": "username", "column": "username"},
            ],
        },
    }
    preview_field = {
        "kind": "related_preview",
        "relation": {
            "db_table": "entries",
            "foreign_key": "collection_id",
            "column": "name",
            "filter_column": "purchased",
            "value": False,
            "limit": 3,
            "output": "name",
            "ordering": [],
        },
    }
    assert serialize_field(session, tables, member_field, keys["a"]) == [
        {"id": 1, "username": "alice"}
    ]
    assert serialize_field(session, tables, preview_field, keys["a"]) == [{"name": "first"}]


def test_parent_duplicate_creator_membership_and_member_actions_are_atomic() -> None:
    session, tables, keys = _database()
    resource = {
        "db_table": "entries",
        "pk_column": "id",
        "columns": [],
    }
    create = {
        "style": "parent_duplicate",
        "parent": {
            "db_table": "collections",
            "pk": "id",
            "pk_kind": "uuid",
            "object_name": "Collection",
        },
        "param": "pk",
        "column": "collection_id",
        "name": "name",
        "flag": "purchased",
        "message": "An unfinished entry already has this name",
    }
    values, error = prepare_parent_create(
        session,
        tables,
        resource,
        create,
        {"pk": str(keys["a"])},
        {"name": "first", "purchased": False},
    )
    assert values == {}
    assert error == (["An unfinished entry already has this name"], 400)
    values, error = prepare_parent_create(
        session,
        tables,
        resource,
        create,
        {"pk": str(keys["a"])},
        {"name": "new", "purchased": False},
    )
    assert error is None and values["collection_id"] == keys["a"]

    collection_access = {"creator_membership": _relation()}
    created = uuid.UUID(int=9)
    session.execute(
        sa.insert(tables["collections"]).values(
            id=created, name="Created", last_activity=dt.datetime(2025, 1, 1)
        )
    )
    add_creator_membership(session, tables, collection_access, created, 1)
    session.commit()
    assert (
        session.execute(
            sa.select(tables["collection_members"].c.account_id).where(
                tables["collection_members"].c.collection_id == created
            )
        ).scalar_one()
        == 1
    )

    contract = {
        "member_action": {
            "action": "add",
            "relation": _relation(),
            "field": "members",
            "touches": ["last_activity"],
            "required": True,
            "allow_empty": False,
            "messages": {
                "required": "This field is required.",
                "null": "This field may not be null.",
                "not_a_list": 'Expected a list of items but got type "{input_type}".',
                "empty": "This list may not be empty.",
            },
            "child_messages": {
                "does_not_exist": 'Invalid pk "{pk_value}" - object does not exist.',
                "incorrect_type": "Incorrect type. Expected pk value, received {data_type}.",
            },
            "user": {"table": "accounts", "pk": "id"},
        }
    }
    collection_resource = {
        "db_table": "collections",
        "pk_column": "id",
        "columns": [{"name": "last_activity", "auto_now": True}],
    }
    instance = (
        session.execute(
            sa.select(tables["collections"]).where(tables["collections"].c.id == keys["a"])
        )
        .mappings()
        .one()
    )
    before = instance["last_activity"]
    payload, status = member_action(
        session, tables, collection_resource, contract, instance, {"members": [2, 999]}
    )
    assert (payload, status) == (
        {"members": ['Invalid pk "999" - object does not exist.']},
        400,
    )
    assert (
        session.execute(
            sa.select(sa.func.count())
            .select_from(tables["collection_members"])
            .where(
                tables["collection_members"].c.collection_id == keys["a"],
                tables["collection_members"].c.account_id == 2,
            )
        ).scalar_one()
        == 0
    )

    payload, status = member_action(
        session, tables, collection_resource, contract, instance, {"members": [2, 2]}
    )
    session.commit()
    assert (payload, status) == ({"members": [1, 2]}, 200)
    changed = session.execute(
        sa.select(tables["collections"].c.last_activity).where(
            tables["collections"].c.id == keys["a"]
        )
    ).scalar_one()
    assert changed > before


def test_generated_membership_matches_source_responses_and_effects(tmp_path: Path) -> None:
    fixture = (
        Path(__file__).parents[2]
        / "sanka-extension-drf-to-fastapi/tests/fixtures/drf_membership_project"
    )
    source = tmp_path / "source"
    shutil.copytree(fixture, source)
    with (source / "board_config/settings.py").open("a") as settings:
        settings.write(
            "\nREST_FRAMEWORK.update({"
            "'DEFAULT_RENDERER_CLASSES':['rest_framework.renderers.JSONRenderer'],"
            "'DEFAULT_PARSER_CLASSES':['rest_framework.parsers.JSONParser']})\n"
        )
    source_db = tmp_path / "source.sqlite3"
    seed_db = tmp_path / "seed.sqlite3"
    capture = r"""
import json, os, shutil, sys, uuid
from pathlib import Path
root, database, seed = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
os.environ['DJANGO_SETTINGS_MODULE'] = 'board_config.settings'
os.environ['SANKA_TEST_DB'] = database
sys.path.insert(0, str(root))
import django
django.setup()
from django.core.management import call_command
call_command('migrate', run_syncdb=True, verbosity=0)
from bulletins.models import Account, Collection, Entry
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient
from sanka_code_migration.drf.models import capture_schema
from sanka_code_migration.drf.scan import scan_django
from sanka_extension_drf_to_flask.sqlalchemy import capture_sqlalchemy_overrides
scan = scan_django(root, settings_module='board_config.settings')
schema, overrides = capture_schema(), capture_sqlalchemy_overrides(scan)
alice = Account.objects.create_user(id=1, username='alice')
bob = Account.objects.create_user(id=2, username='bob')
admin = Account.objects.create_superuser(id=3, username='admin', password='test')
tokens = {'alice':'a' * 40, 'bob':'b' * 40, 'admin':'c' * 40}
for user in (alice, bob, admin):
    Token.objects.create(user=user, key=tokens[user.username])
a, b = uuid.UUID(int=1), uuid.UUID(int=2)
ca = Collection.objects.create(id=a, name='A')
ca.members.add(alice)
cb = Collection.objects.create(id=b, name='B')
cb.members.add(bob)
Entry.objects.create(id=uuid.UUID(int=3), name='first', purchased=False, collection=ca)
Entry.objects.create(id=uuid.UUID(int=4), name='done', purchased=True, collection=ca)
Entry.objects.create(id=uuid.UUID(int=5), name='second', purchased=False, collection=cb)
from django.db import connections
connections.close_all()
shutil.copyfile(database, seed)
missing = str(uuid.UUID(int=99))
cases = [
 {'method':'get','user':'alice','path':'/collections/'},
 {'method':'get','user':'alice','path':f'/collections/{a}/'},
 {'method':'get','user':'alice','path':f'/collections/{b}/'},
 {'method':'get','user':None,'path':f'/collections/{missing}/'},
 {'method':'get','user':'alice','path':f'/collections/{a}/entries/{uuid.UUID(int=5)}/'},
 {'method':'get','user':'alice','path':'/search/'},
 {'method':'post','user':'alice','path':f'/collections/{a}/entries/',
  'json':{'name':'first','purchased':False}},
 {'method':'post','user':'alice','path':f'/collections/{a}/entries/',
  'json':{'name':'new','purchased':False,'collection':str(b)}},
 {'method':'post','user':'alice','path':'/collections/','json':{'name':'Created'}},
 {'method':'put','user':'alice','path':f'/collections/{a}/add/','json':{'members':[2,999]}},
 {'method':'put','user':'bob','path':f'/collections/{a}/add/','raw':'{'},
 {'method':'put','user':'alice','path':f'/collections/{a}/add/','json':{'members':[2,2]}},
 {'method':'put','user':'bob','path':f'/collections/{a}/remove/','json':{'members':[1]}},
 {'method':'get','user':'alice','path':f'/collections/{a}/'},
 {'method':'post','user':'admin','path':f'/collections/{missing}/entries/',
  'json':{'name':'ghost','purchased':False}},
]
def run(case):
    client = APIClient()
    client.raise_request_exception = False
    if case['user']:
        client.credentials(HTTP_AUTHORIZATION='Token ' + tokens[case['user']])
    if 'raw' in case:
        response = client.generic(case['method'].upper(), case['path'], case['raw'],
                                  content_type='application/json')
    elif 'json' in case:
        response = getattr(client, case['method'])(case['path'], case['json'], format='json')
    else:
        response = getattr(client, case['method'])(case['path'])
    media = response.get('Content-Type','').split(';')[0]
    body = (json.loads(response.content) if media == 'application/json'
            else response.content.decode()) if response.content else None
    if response.status_code == 201:
        body['id'] = '<generated>'
    return {'status':response.status_code, 'media':media, 'body':body}
responses = [run(case) for case in cases]
state = {
 'collections': sorted(
     (row.name, tuple(row.members.order_by('username').values_list('username', flat=True)))
     for row in Collection.objects.all()),
 'entries': sorted(Entry.objects.values_list('name','purchased','collection__name')),
}
print(json.dumps({'scan':scan.to_dict(),'schema':schema,'overrides':overrides,
                  'cases':cases,'responses':responses,'state':state,'tokens':tokens}))
"""
    result = subprocess.run(
        [sys.executable, "-c", capture, str(source), str(source_db), str(seed_db)],
        env=dict(os.environ),
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(result.stdout)
    scan = FrameworkScan.from_dict(facts["scan"])
    assert not facts["schema"]["gaps"], facts["schema"]["gaps"]
    qualified = qualify_routes(scan, facts["overrides"])
    assert all(row["native"] for row in qualified), [row for row in qualified if not row["native"]]
    operations = {operation.name for operation in _service_operations(scan, facts["schema"])}
    assert {
        "create-membership:bulletins.views.Collections",
        "member-add:bulletins.views.AddMember",
        "member-remove:bulletins.views.RemoveMember",
    } <= operations

    files = {
        **render_database(facts["schema"]),
        **render_sqlalchemy(scan, facts["schema"], overrides=facts["overrides"]),
    }
    target = tmp_path / "target"
    for name, content in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    migration_db = target / "migration.sqlite3"
    env = dict(os.environ) | {"SANKA_DATABASE_URL": "sqlite:///" + str(migration_db)}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=target,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    target_db = target / "target.sqlite3"
    shutil.copyfile(seed_db, target_db)
    (target / "reference.json").write_text(
        json.dumps({key: facts[key] for key in ("cases", "responses", "state", "tokens")})
    )
    probe = r'''
import importlib.abc, json, os, sys
from pathlib import Path
from sqlalchemy import create_engine, text
reference = json.loads(Path('reference.json').read_text())
class NoSource(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in {'django','rest_framework','bulletins','board_config',
                                  'sanka_code_migration','sanka_extension_drf_to_flask'}:
            raise ImportError('source dependency forbidden: ' + name)
sys.meta_path.insert(0, NoSource())
from target_app import create_app
client = create_app({'TESTING':True}).test_client()
actual = []
for case in reference['cases']:
    headers = ({'Authorization':'Token ' + reference['tokens'][case['user']]}
               if case['user'] else {})
    if 'raw' in case:
        response = client.open(case['path'], method=case['method'].upper(), data=case['raw'],
                               content_type='application/json', headers=headers)
    else:
        response = client.open(case['path'], method=case['method'].upper(),
                               json=case.get('json'), headers=headers)
    media = response.content_type.split(';')[0]
    body = (response.get_json() if media == 'application/json'
            else response.get_data(as_text=True)) if response.data else None
    if response.status_code == 201:
        body['id'] = '<generated>'
    actual.append({'status':response.status_code,'media':media,'body':body})
assert actual == reference['responses'], list(zip(reference['cases'],reference['responses'],actual))
engine = create_engine(os.environ['SANKA_DATABASE_URL'])
with engine.connect() as connection:
    memberships = connection.execute(text("""SELECT c.name,a.username
      FROM bulletins_collection c LEFT JOIN bulletins_collection_members m ON m.collection_id=c.id
      LEFT JOIN bulletins_account a ON a.id=m.account_id ORDER BY c.name,a.username""")).all()
    entries = connection.execute(text("""SELECT e.name,e.purchased,c.name
      FROM bulletins_entry e JOIN bulletins_collection c ON c.id=e.collection_id
      ORDER BY e.name,e.purchased,c.name""")).all()
grouped = {}
for name, username in memberships:
    grouped.setdefault(name, []).append(username)
state = {'collections':sorted([name,users] for name,users in grouped.items()),
         'entries':sorted([name,bool(value),parent] for name,value,parent in entries)}
assert state == reference['state'], (state, reference['state'])
'''
    env["SANKA_DATABASE_URL"] = "sqlite:///" + str(target_db)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=target,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
