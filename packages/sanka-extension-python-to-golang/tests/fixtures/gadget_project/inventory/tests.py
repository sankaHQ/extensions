# SPDX-License-Identifier: Apache-2.0
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import Gadget


class GadgetApiTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.gadget = Gadget.objects.create(name="Alpha", quantity=3, notes="Ready")

    def test_list(self) -> None:
        response = self.client.get("/api/gadgets/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [{"id": self.gadget.id, "name": "Alpha", "quantity": 3, "notes": "Ready"}],
        )

    def test_create(self) -> None:
        response = self.client.post(
            "/api/gadgets/",
            {"name": "Beta", "quantity": 7, "notes": "Backorder"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Gadget.objects.count(), 2)

    def test_rejects_negative_quantity(self) -> None:
        response = self.client.post(
            "/api/gadgets/",
            {"name": "Beta", "quantity": -1},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Gadget.objects.count(), 1)
