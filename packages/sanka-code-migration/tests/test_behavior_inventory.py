# SPDX-License-Identifier: Apache-2.0
import pytest
from sanka_code_migration.drf.inventory import inventory_behavior


def test_non_http_behavior_is_not_silently_dropped(tmp_path):
    (tmp_path / "models.py").write_text(
        "from django.db import models\n"
        "from django.db.models.signals import post_save as saved\n"
        "class Base(models.Model): pass\n"
        "class Item(Base):\n"
        "    def save(self, *args, **kwargs): pass\n"
        "def changed(sender, **kwargs): pass\n"
        "saved.connect(changed)\n"
    )
    (tmp_path / "tasks.py").write_text("@shared_task\ndef send_notice(): pass\n")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations/0002_data.py").write_text("op = migrations.RunPython(seed)\n")
    (tmp_path / ".sanka").mkdir()
    (tmp_path / ".sanka/generated.py").write_text("@shared_task\ndef ignored(): pass\n")
    report = inventory_behavior(tmp_path)
    assert {item["feature"] for item in report} == {
        "model-lifecycle-hook",
        "signal-handler",
        "background-task",
        "data-migration",
    }
    assert all(not item["source"].startswith(str(tmp_path)) for item in report)
    assert report == inventory_behavior(tmp_path)


@pytest.mark.parametrize(
    "source, feature, line",
    [
        (
            "from django.db.models.expressions import RawSQL as SQL\n"
            "result = SQL('select 1', [])\n"
            "",
            "raw-sql",
            2,
        ),
        ("def read():\n    return Item.objects.raw('select * from items')\n", "raw-sql", 2),
        (
            "from django.db import connection as db\n"
            "with db.cursor() as cur:\n"
            "    cur.execute('select 1')\n"
            "",
            "raw-sql",
            3,
        ),
        (
            "from django.db import connection\n"
            "cur = connection.cursor()\n"
            "cur.executemany('insert x values (%s)', [(1,)])\n"
            "",
            "raw-sql",
            3,
        ),
        (
            "import requests as http\nhttp.post('https://example.invalid')\n",
            "external-network-call",
            2,
        ),
        (
            "from httpx import get as fetch\nfetch('https://example.invalid')\n",
            "external-network-call",
            2,
        ),
        (
            "import httpx\nclient = httpx.Client()\nclient.get('https://example.invalid')\n",
            "external-network-call",
            3,
        ),
        (
            "import httpx as http\n"
            "with http.AsyncClient() as client:\n"
            "    client.post('https://example.invalid')\n"
            "",
            "external-network-call",
            3,
        ),
        (
            "def fetch():\n"
            "    from urllib.request import urlopen as open_url\n"
            "    return open_url('https://example.invalid')\n"
            "",
            "external-network-call",
            3,
        ),
        (
            "from urllib import request as req\n"
            "opener = req.build_opener()\n"
            "opener.open('https://example.invalid')\n"
            "",
            "external-network-call",
            3,
        ),
        (
            "from django.core.mail import send_mail as mail\nmail('title', 'body', 'a', ['b'])\n",
            "email-operation",
            2,
        ),
        (
            "from django.core.mail import EmailMessage as Message\n"
            "message = Message('title', 'body')\n"
            "message.send()\n"
            "",
            "email-operation",
            3,
        ),
        ("import smtplib as mail\nmail.SMTP('mail.example.invalid')\n", "email-operation", 2),
        ("STORAGES = {'default': {'BACKEND': 'custom.Storage'}}\n", "storage-configuration", 1),
        ("DEFAULT_FILE_STORAGE = 'custom.Storage'\n", "storage-configuration", 1),
        (
            "from django.core.files.storage import default_storage as store\n"
            "store.save('name', content)\n"
            "",
            "storage-operation",
            2,
        ),
        (
            "from django.core.files.storage import FileSystemStorage as Files\n"
            "store = Files()\n"
            "store.delete('name')\n"
            "",
            "storage-operation",
            3,
        ),
        (
            "from django.core.files.storage import storages as stores\n"
            "stores['private'].open('name')\n"
            "",
            "storage-operation",
            2,
        ),
    ],
)
def test_known_external_behavior_is_reported_without_execution(tmp_path, source, feature, line):
    (tmp_path / "effects.py").write_text(
        source + "\nraise RuntimeError('must not execute source')\n"
    )
    assert inventory_behavior(tmp_path) == [
        {"source": "effects.py", "line": line, "feature": feature}
    ]


def test_ordinary_dictionary_and_orm_calls_are_not_external_effects(tmp_path):
    (tmp_path / "ordinary.py").write_text("""
import httpx
client = httpx.Client()
value = {'key': 1}.get('key')
rows = Product.objects.filter(active=True).order_by('id')
first = rows.get(id=1)
product.save()
""")
    assert inventory_behavior(tmp_path) == []
