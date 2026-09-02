from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [
        migrations.CreateModel(
            name="Record",
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
                ("label", models.CharField(max_length=80)),
                ("category", models.CharField(max_length=30)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("posted_at", models.DateTimeField()),
            ],
            options={"ordering": ["-posted_at", "-id"]},
        )
    ]
