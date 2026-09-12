from django.urls import include, path
from orders.views import OrderViewSet
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register("orders", OrderViewSet, basename="order")

urlpatterns = [path("api/", include(router.urls))]
