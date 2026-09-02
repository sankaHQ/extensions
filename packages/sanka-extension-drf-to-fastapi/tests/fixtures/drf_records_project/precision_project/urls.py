from django.urls import include, path
from records.views import RecordViewSet
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register("records", RecordViewSet, basename="record")

urlpatterns = [path("api/", include(router.urls))]
