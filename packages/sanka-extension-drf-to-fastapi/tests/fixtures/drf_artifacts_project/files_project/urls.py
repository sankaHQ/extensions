from artifacts.views import ArtifactViewSet
from django.urls import path

collection = ArtifactViewSet.as_view({"get": "list", "post": "create"})
detail = ArtifactViewSet.as_view({"get": "retrieve"})
download = ArtifactViewSet.as_view({"get": "download"})

urlpatterns = [
    path("api/files/", collection, name="artifact-list"),
    path(
        "api/files.json",
        collection,
        {"format": "json"},
        name="artifact-list-json",
    ),
    path(
        "api/files.api",
        collection,
        {"format": "api"},
        name="artifact-list-api",
    ),
    path("api/files/<int:pk>/", detail, name="artifact-detail"),
    path(
        "api/files/<int:pk>.json",
        detail,
        {"format": "json"},
        name="artifact-detail-json",
    ),
    path(
        "api/files/<int:pk>.api",
        detail,
        {"format": "api"},
        name="artifact-detail-api",
    ),
    path("api/files/<int:pk>/download/", download, name="artifact-download"),
    path(
        "api/files/<int:pk>/download.json",
        download,
        {"format": "json"},
        name="artifact-download-json",
    ),
    path(
        "api/files/<int:pk>/download.api",
        download,
        {"format": "api"},
        name="artifact-download-api",
    ),
]
