import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Order",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("reference", models.CharField(max_length=30, unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("new", "New"), ("paid", "Paid"), ("cancelled", "Cancelled")],
                        default="new",
                        max_length=10,
                    ),
                ),
                ("memo", models.CharField(blank=True, default="", max_length=200)),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="OrderItem",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("sku", models.CharField(max_length=30)),
                ("quantity", models.PositiveIntegerField()),
                ("price", models.DecimalField(decimal_places=2, max_digits=8)),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="orders.order",
                    ),
                ),
            ],
            options={"ordering": ["id"]},
        ),
    ]
