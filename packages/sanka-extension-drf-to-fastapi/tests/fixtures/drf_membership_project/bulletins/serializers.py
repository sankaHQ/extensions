# SPDX-License-Identifier: Apache-2.0
from rest_framework import serializers

from .models import Account, Collection, Entry


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = ["id", "username"]


class CollectionSerializer(serializers.ModelSerializer):
    members = AccountSerializer(many=True, read_only=True)
    preview = serializers.SerializerMethodField()

    def get_preview(self, obj):
        return [{"name": entry.name} for entry in obj.entries.filter(purchased=False)][:3]

    class Meta:
        model = Collection
        fields = ["id", "name", "members", "preview"]


class EntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Entry
        fields = ["id", "name", "purchased", "collection"]
        read_only_fields = ["id", "collection"]

    def create(self, validated_data, **kwargs):
        validated_data["collection_id"] = self.context["request"].parser_context["kwargs"]["pk"]
        if Collection.objects.get(
            id=self.context["request"].parser_context["kwargs"]["pk"]
        ).entries.filter(name=validated_data["name"], purchased=False):
            raise serializers.ValidationError("An unfinished entry already has this name")
        return super().create(validated_data)


class AddMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = Collection
        fields = ["members"]

    def update(self, instance, validated_data):
        for member in validated_data["members"]:
            instance.members.add(member)
            instance.save()
        return instance


class RemoveMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = Collection
        fields = ["members"]

    def update(self, instance, validated_data):
        for member in validated_data["members"]:
            instance.members.remove(member)
            instance.save()
        return instance
