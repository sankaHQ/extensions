# SPDX-License-Identifier: Apache-2.0
from rest_framework import generics, status
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Collection, Entry
from .permissions import CollectionMembers, EntryMembers, ParentMembers
from .serializers import (
    AddMemberSerializer,
    CollectionSerializer,
    EntrySerializer,
    RemoveMemberSerializer,
)


class Collections(generics.ListCreateAPIView):
    serializer_class = CollectionSerializer

    def get_queryset(self):
        return Collection.objects.filter(members=self.request.user).order_by("-last_activity")

    def perform_create(self, serializer):
        instance = serializer.save()
        instance.members.add(self.request.user)
        return instance


class CollectionDetail(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = CollectionSerializer
    queryset = Collection.objects.all()
    permission_classes = [CollectionMembers]


class Entries(generics.ListCreateAPIView):
    serializer_class = EntrySerializer
    permission_classes = [ParentMembers]

    def get_queryset(self):
        parent = self.kwargs["pk"]
        queryset = Entry.objects.filter(collection=parent).order_by("purchased")
        return queryset


class EntryDetail(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = EntrySerializer
    permission_classes = [EntryMembers]
    lookup_url_kwarg = "item_pk"

    def get_queryset(self):
        return Entry.objects.filter(collection_id=self.kwargs["pk"])


class SearchEntries(generics.ListAPIView):
    serializer_class = EntrySerializer

    def get_queryset(self):
        parents = Collection.objects.filter(members=self.request.user)
        result = Entry.objects.filter(collection__in=parents).order_by("name")
        return result


class AddMember(APIView):
    permission_classes = [CollectionMembers]

    def put(self, request, pk, format=None):
        parent = get_object_or_404(Collection, pk=pk)
        serializer = AddMemberSerializer(parent, data=request.data)
        self.check_object_permissions(request, parent)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RemoveMember(APIView):
    permission_classes = [CollectionMembers]

    def put(self, request, pk, format=None):
        parent = get_object_or_404(Collection, pk=pk)
        serializer = RemoveMemberSerializer(parent, data=request.data)
        self.check_object_permissions(request, parent)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
