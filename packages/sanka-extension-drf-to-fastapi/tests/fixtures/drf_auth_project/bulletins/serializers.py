# SPDX-License-Identifier: Apache-2.0
from rest_framework import serializers

from bulletins.models import Bulletin


class BulletinSerializer(serializers.ModelSerializer):
    class Meta:
        model = Bulletin
        fields = ("id", "author", "title", "body")
        read_only_fields = ("id", "author")
