from __future__ import annotations

from django.http import FileResponse, HttpResponse
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.viewsets import ModelViewSet

from artifacts.models import Artifact
from artifacts.serializers import ArtifactSerializer


class ArtifactViewSet(ModelViewSet):
    queryset = Artifact.objects.all()
    serializer_class = ArtifactSerializer
    parser_classes = [MultiPartParser, FormParser]

    @action(detail=True, methods=["get"])
    def download(
        self,
        request: Request,
        pk: str | None = None,
        **kwargs: str,
    ) -> HttpResponse:
        artifact = self.get_object()
        return FileResponse(
            artifact.file.open("rb"),
            as_attachment=True,
            filename=artifact.original_name,
            content_type=artifact.content_type,
        )
