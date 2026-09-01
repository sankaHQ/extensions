# SPDX-License-Identifier: Apache-2.0
from rest_framework.viewsets import ModelViewSet

from inventory.models import Gadget
from inventory.serializers import GadgetSerializer


class GadgetViewSet(ModelViewSet):
    queryset = Gadget.objects.all()
    serializer_class = GadgetSerializer
