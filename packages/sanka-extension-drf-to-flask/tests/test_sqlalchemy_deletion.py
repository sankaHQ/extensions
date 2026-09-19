# SPDX-License-Identifier: Apache-2.0
"""Delete effects and rollback are compared with Django's real Collector."""

import os
import subprocess
import sys


def test_collected_deletion_matches_django(tmp_path):
    app = tmp_path / "sample"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (app / "models.py").write_text("""from django.db import models
class Parent(models.Model):
    label = models.CharField(max_length=20)
class Child(models.Model):
    parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
class Guard(models.Model):
    parent = models.ForeignKey(Parent, on_delete=models.CASCADE)
    child = models.ForeignKey(Child, on_delete=models.RESTRICT)
class Protected(models.Model):
    parent = models.ForeignKey(Parent, on_delete=models.PROTECT)
class Nullable(models.Model):
    parent = models.ForeignKey(Parent, on_delete=models.SET_NULL, null=True)
class UnmanagedDelete(models.Model):
    parent = models.ForeignKey(Parent, on_delete=models.DO_NOTHING)
class Branch(models.Model):
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True)
""")
    (tmp_path / "settings.py").write_text(
        "SECRET_KEY='test'\nINSTALLED_APPS=['sample']\n"
        "DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':'source.db'}}\n"
    )
    script = """
import django, shutil, pathlib
django.setup()
from django.db import connection, transaction, IntegrityError
from django.db.models.deletion import ProtectedError, RestrictedError
from sample.models import Parent, Child, Guard, Protected, Nullable, UnmanagedDelete, Branch
from sanka_code_migration.drf.models import capture_schema
from sanka_extension_drf_to_flask.sqlalchemy_deletion import delete_instance, DeletionBlocked
import sqlalchemy as sa
from sqlalchemy.orm import Session
models = [Parent, Child, Guard, Protected, Nullable, UnmanagedDelete, Branch]
schema = capture_schema(models)
for case in ['cascade', 'restrict_collected', 'restrict_external', 'protect', 'do_nothing',
             'self_cascade']:
 connection.close()
 pathlib.Path('source.db').unlink(missing_ok=True)
 with connection.schema_editor() as editor:
  for model in models:
   editor.create_model(model)
 parent = Parent.objects.create(label='first')
 other = Parent.objects.create(label='second')
 child = Child.objects.create(parent=parent)
 Nullable.objects.create(parent=parent)
 branch = Branch.objects.create()
 Branch.objects.create(parent=branch)
 if case.startswith('restrict'):
  Guard.objects.create(parent=parent if case == 'restrict_collected' else other, child=child)
 if case == 'protect': Protected.objects.create(parent=parent)
 if case == 'do_nothing': UnmanagedDelete.objects.create(parent=parent)
 connection.close()
 shutil.copyfile('source.db', 'candidate.db')
 target = branch if case == 'self_cascade' else parent
 target_pk = target.pk
 try:
  with transaction.atomic(): target.delete()
  source_ok = True
 except (ProtectedError, RestrictedError, IntegrityError):
  source_ok = False
 expected = {m._meta.db_table:list(m.objects.order_by('pk').values()) for m in models}
 engine = sa.create_engine('sqlite:///candidate.db')
 @sa.event.listens_for(engine, 'connect')
 def foreign_keys(dbapi, record): dbapi.execute('PRAGMA foreign_keys=ON')
 metadata = sa.MetaData()
 metadata.reflect(engine)
 with Session(engine) as session:
  table_name = 'sample_branch' if case == 'self_cascade' else 'sample_parent'
  table = metadata.tables[table_name]
  instance = session.execute(sa.select(table).where(table.c.id == target_pk)).mappings().one()
  try:
   delete_instance(session, metadata.tables, schema, table_name, instance)
   session.commit()
   candidate_ok = True
  except (DeletionBlocked, sa.exc.IntegrityError):
   session.rollback()
   candidate_ok = False
  actual = {name:[dict(r) for r in session.execute(sa.select(t).order_by(t.c.id)).mappings()]
            for name,t in metadata.tables.items()}
 assert candidate_ok == source_ok, (case, source_ok, candidate_ok)
 assert actual == expected, (case, expected, actual)
 engine.dispose()
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ | {"DJANGO_SETTINGS_MODULE": "settings"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
