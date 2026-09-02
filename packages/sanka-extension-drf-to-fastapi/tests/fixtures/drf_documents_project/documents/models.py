from django.conf import settings
from django.db import models


class AccessToken(models.Model):
    key = models.CharField(max_length=40, primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bench_access_tokens",
    )
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["key"]


class Document(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bench_documents",
    )
    title = models.CharField(max_length=100)
    body = models.CharField(max_length=400, blank=True, default="")
    reviewed = models.BooleanField(default=False)

    class Meta:
        ordering = ["id"]
