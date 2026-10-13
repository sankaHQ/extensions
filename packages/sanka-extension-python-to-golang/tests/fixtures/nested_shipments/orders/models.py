# SPDX-License-Identifier: Apache-2.0
from django.db import models


class Shipment(models.Model):
    reference = models.CharField(max_length=30, unique=True)
    memo = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["id"]


class Parcel(models.Model):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="parcels")
    label = models.CharField(max_length=30)
    quantity = models.PositiveIntegerField()

    class Meta:
        ordering = ["id"]
        unique_together = [("shipment", "label")]


class Charge(models.Model):
    parcel = models.ForeignKey(Parcel, on_delete=models.CASCADE, related_name="charges")
    code = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=8, decimal_places=2)

    class Meta:
        ordering = ["id"]
        unique_together = [("parcel", "code")]
