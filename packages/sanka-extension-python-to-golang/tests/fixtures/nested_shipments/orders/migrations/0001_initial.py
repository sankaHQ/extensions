# SPDX-License-Identifier: Apache-2.0
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Shipment",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("reference", models.CharField(max_length=30, unique=True)),
                (
                    "memo",
                    models.TextField(
                        blank=True,
                        default="",
                        choices=[("", "Unspecified"), ("updated", "Updated note")],
                    ),
                ),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="Parcel",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "shipment",
                    models.ForeignKey(
                        to="orders.shipment",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="parcels",
                    ),
                ),
                ("label", models.CharField(max_length=30)),
                ("quantity", models.PositiveIntegerField()),
            ],
            options={"ordering": ["id"], "unique_together": [("shipment", "label")]},
        ),
        migrations.CreateModel(
            name="Charge",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "parcel",
                    models.ForeignKey(
                        to="orders.parcel",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="charges",
                    ),
                ),
                ("code", models.CharField(max_length=20)),
                ("amount", models.DecimalField(max_digits=8, decimal_places=2)),
            ],
            options={"ordering": ["id"], "unique_together": [("parcel", "code")]},
        ),
    ]
