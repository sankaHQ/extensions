# SPDX-License-Identifier: Apache-2.0
from rest_framework.viewsets import ModelViewSet

from orders.models import Shipment
from orders.serializers import ShipmentSerializer


class ShipmentViewSet(ModelViewSet):
    queryset = Shipment.objects.prefetch_related("parcels__charges").all()
    serializer_class = ShipmentSerializer
