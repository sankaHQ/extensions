from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from documents.authentication import ExpiringTokenAuthentication
from documents.models import Document
from documents.permissions import IsDocumentOwner
from documents.serializers import DocumentSerializer


class DocumentViewSet(ModelViewSet):
    queryset = Document.objects.all()
    serializer_class = DocumentSerializer
    authentication_classes = [ExpiringTokenAuthentication, SessionAuthentication]

    def get_permissions(self):
        if self.action == "list":
            permission_classes = [AllowAny]
        elif self.action == "create":
            permission_classes = [IsAuthenticated]
        elif self.action in {"destroy", "review"}:
            permission_classes = [IsAdminUser]
        else:
            permission_classes = [IsAuthenticated, IsDocumentOwner]
        return [permission() for permission in permission_classes]

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        document = self.get_object()
        document.reviewed = True
        document.save(update_fields=["reviewed"])
        return Response(self.get_serializer(document).data)
