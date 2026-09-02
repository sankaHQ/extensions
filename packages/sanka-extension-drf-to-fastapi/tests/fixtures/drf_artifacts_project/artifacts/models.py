from django.db import models


class Artifact(models.Model):
    key = models.SlugField(max_length=80, unique=True)
    label = models.CharField(max_length=120)
    file = models.FileField(upload_to="uploads/")
    original_name = models.CharField(max_length=180)
    content_type = models.CharField(max_length=100)
    byte_size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)

    class Meta:
        ordering = ["id"]
