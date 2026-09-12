from django.test import TestCase
from rest_framework.test import APIClient

from orders.models import Order, OrderItem


class OrderApiTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.order = Order.objects.create(reference="ORD-1", status="new")
        OrderItem.objects.create(order=self.order, sku="SKU-A", quantity=2, price="10.00")

    def test_nested_create(self) -> None:
        response = self.client.post(
            "/api/orders/",
            {
                "reference": "ORD-2",
                "status": "new",
                "items": [{"sku": "SKU-B", "quantity": 3, "price": "4.50"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["items"][0]["price"], "4.50")
        self.assertEqual(OrderItem.objects.count(), 2)

    def test_business_rule_rolls_back_the_whole_order(self) -> None:
        response = self.client.post(
            "/api/orders/",
            {
                "reference": "ORD-3",
                "items": [
                    {"sku": "SKU-B", "quantity": 60, "price": "1.00"},
                    {"sku": "SKU-C", "quantity": 60, "price": "1.00"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Order.objects.filter(reference="ORD-3").exists())
        self.assertEqual(OrderItem.objects.count(), 1)

    def test_duplicate_reference_rejected(self) -> None:
        response = self.client.post(
            "/api/orders/",
            {"reference": "ORD-1", "items": []},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 1)
