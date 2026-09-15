# SPDX-License-Identifier: Apache-2.0
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import BasePermission

from .models import Collection

# ruff: noqa: SIM103 -- Preserve the permission idiom under test.


class CollectionMembers(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.user.is_superuser:
            return True
        if request.user in obj.members.all():
            return True
        return False


class EntryMembers(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.user.is_superuser:
            return True
        if request.user in obj.collection.members.all():
            return True
        return False


class ParentMembers(BasePermission):
    def has_permission(self, request, view):
        if request.user.is_superuser:
            return True
        parent = get_object_or_404(Collection, pk=view.kwargs.get("pk"))
        if request.user in parent.members.all():
            return True
        return False
