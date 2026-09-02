from django.db import models


class Record(models.Model):
    label = models.CharField(max_length=80)
    category = models.CharField(max_length=30)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    posted_at = models.DateTimeField()

    class Meta:
        ordering = ["-posted_at", "-id"]
