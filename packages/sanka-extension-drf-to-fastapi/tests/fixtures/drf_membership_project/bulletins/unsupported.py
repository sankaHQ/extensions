# SPDX-License-Identifier: Apache-2.0
"""Deliberately unsupported shapes: conversion must reject, never discard rules."""

from rest_framework.permissions import BasePermission

from .models import Collection
from .serializers import AddMemberSerializer, EntrySerializer
from .views import Collections


class StaffMembers(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.user.is_staff:
            return True
        return request.user in obj.members.all()


class SideEffectQuery(Collections):
    def get_queryset(self):
        self.request.user.save()
        return Collection.objects.filter(members=self.request.user)


class DifferentPredicate(Collections):
    def get_queryset(self):
        return Collection.objects.filter(members__is_staff=True)


class ExtraCreate(Collections):
    def perform_create(self, serializer):
        instance = serializer.save()
        instance.members.add(self.request.user)
        instance.delete()
        return instance


class ExtraUpdate(AddMemberSerializer):
    def update(self, instance, validated_data):
        instance.members.clear()
        for member in validated_data["members"]:
            instance.members.add(member)
            instance.save()
        return instance


class DifferentDuplicate(EntrySerializer):
    def create(self, validated_data, **kwargs):
        validated_data["collection_id"] = self.context["request"].parser_context["kwargs"]["pk"]
        if Collection.objects.get(
            id=self.context["request"].parser_context["kwargs"]["pk"]
        ).entries.filter(name=validated_data["name"], purchased=True):
            raise ValueError("custom failure")
        return super().create(validated_data)


class ValidatedMembers(AddMemberSerializer):
    def validate_members(self, members):
        raise ValueError("Needs an invitation")


class CustomInputMembers(AddMemberSerializer):
    def to_internal_value(self, data):
        return super().to_internal_value(data)


class CustomListValidation(AddMemberSerializer):
    class Meta(AddMemberSerializer.Meta):
        validators = [lambda value: value]
