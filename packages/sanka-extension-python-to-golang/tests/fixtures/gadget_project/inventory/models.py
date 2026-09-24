# SPDX-License-Identifier: Apache-2.0
from django.db import models


class Gadget(models.Model):
    name = models.CharField(max_length=80)
    quantity = models.PositiveIntegerField(default=0)
    notes = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        ordering = ("id",)
