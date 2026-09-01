# SPDX-License-Identifier: Apache-2.0
from rest_framework import serializers

from inventory.models import Gadget


class GadgetSerializer(serializers.ModelSerializer):
    quantity = serializers.IntegerField(min_value=0)

    class Meta:
        model = Gadget
        fields = ("id", "name", "quantity", "notes")
        read_only_fields = ("id",)
