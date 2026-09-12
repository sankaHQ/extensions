from django.db import models


class Order(models.Model):
    STATUS_CHOICES = [
        ("new", "New"),
        ("paid", "Paid"),
        ("cancelled", "Cancelled"),
    ]

    reference = models.CharField(max_length=30, unique=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="new")
    memo = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["id"]


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    sku = models.CharField(max_length=30)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=8, decimal_places=2)

    class Meta:
        ordering = ["id"]
