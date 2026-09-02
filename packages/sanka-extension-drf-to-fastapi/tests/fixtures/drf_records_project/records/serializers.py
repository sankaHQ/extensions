from rest_framework import serializers

from records.models import Record


class RecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = Record
        fields = ["id", "label", "category", "amount", "posted_at"]
        read_only_fields = ["id"]
