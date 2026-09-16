# SPDX-License-Identifier: Apache-2.0
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
