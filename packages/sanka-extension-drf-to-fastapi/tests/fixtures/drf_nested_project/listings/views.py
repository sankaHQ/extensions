# SPDX-License-Identifier: Apache-2.0
from rest_framework.viewsets import ModelViewSet

from listings.models import Listing
from listings.serializers import ListingSerializer


class ListingViewSet(ModelViewSet):
    queryset = Listing.objects.all()
    serializer_class = ListingSerializer
