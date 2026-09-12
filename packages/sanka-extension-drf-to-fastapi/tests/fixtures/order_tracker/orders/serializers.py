from django.db import transaction
from rest_framework import serializers

from orders.models import Order, OrderItem


class OrderItemSerializer(serializers.ModelSerializer):
    quantity = serializers.IntegerField(min_value=1)

    class Meta:
        model = OrderItem
        fields = ["id", "sku", "quantity", "price"]
        read_only_fields = ["id"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True)

    class Meta:
        model = Order
        fields = ["id", "reference", "status", "memo", "items"]
        read_only_fields = ["id"]

    def create(self, validated_data):
        items_data = validated_data.pop("items")
        with transaction.atomic():
            order = Order.objects.create(**validated_data)
            for item_data in items_data:
                OrderItem.objects.create(order=order, **item_data)
            total = sum(item.quantity for item in order.items.all())
            if total > 100:
                raise serializers.ValidationError({"items": ["Order exceeds 100 total units."]})
        return order

    def update(self, instance, validated_data):
        validated_data.pop("items", None)
        return super().update(instance, validated_data)
