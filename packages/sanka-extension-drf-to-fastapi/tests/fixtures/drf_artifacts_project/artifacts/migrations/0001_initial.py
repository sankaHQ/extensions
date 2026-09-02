from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [
        migrations.CreateModel(
            name="Artifact",
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
                ("key", models.SlugField(max_length=80, unique=True)),
                ("label", models.CharField(max_length=120)),
                ("file", models.FileField(upload_to="uploads/")),
                ("original_name", models.CharField(max_length=180)),
                ("content_type", models.CharField(max_length=100)),
                ("byte_size", models.PositiveIntegerField()),
                ("sha256", models.CharField(max_length=64)),
            ],
            options={"ordering": ["id"]},
        )
    ]
