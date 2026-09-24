# SPDX-License-Identifier: Apache-2.0
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from inventory.views import GadgetViewSet

router = DefaultRouter()
router.register("gadgets", GadgetViewSet, basename="gadget")

urlpatterns = [path("api/", include(router.urls))]
