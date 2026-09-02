from django.urls import include, path
from documents.views import DocumentViewSet
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register("documents", DocumentViewSet, basename="document")

urlpatterns = [path("api/", include(router.urls))]
