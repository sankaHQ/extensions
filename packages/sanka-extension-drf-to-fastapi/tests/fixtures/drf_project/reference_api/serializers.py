# SPDX-License-Identifier: Apache-2.0
from rest_framework import serializers

from reference_api.models import Project


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ("id", "name")
