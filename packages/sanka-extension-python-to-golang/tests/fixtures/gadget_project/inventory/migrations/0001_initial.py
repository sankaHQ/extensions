# SPDX-License-Identifier: Apache-2.0
from typing import ClassVar

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies: ClassVar[list[tuple[str, str]]] = []
    operations: ClassVar[list[object]] = [
        migrations.CreateModel(
            name="Gadget",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=80)),
                ("quantity", models.PositiveIntegerField(default=0)),
                ("notes", models.CharField(blank=True, default="", max_length=120)),
            ],
            options={"ordering": ("id",)},
        )
    ]
