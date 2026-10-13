# SPDX-License-Identifier: Apache-2.0
from django.urls import include, path
from orders.views import ShipmentViewSet
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register("orders", ShipmentViewSet, basename="order")

urlpatterns = [path("api/", include(router.urls))]
