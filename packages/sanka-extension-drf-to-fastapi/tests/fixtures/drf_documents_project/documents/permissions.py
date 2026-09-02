from rest_framework.permissions import BasePermission


class IsDocumentOwner(BasePermission):
    """Object checks apply only after a detail object has been loaded."""

    def has_object_permission(self, request, view, obj):
        return obj.owner_id == request.user.id
