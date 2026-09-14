# SPDX-License-Identifier: Apache-2.0
from bulletins.views import (
    AddMember,
    CollectionDetail,
    Collections,
    Entries,
    EntryDetail,
    RemoveMember,
    SearchEntries,
)
from django.urls import path

urlpatterns = [
    path("collections/", Collections.as_view()),
    path("collections/<uuid:pk>/", CollectionDetail.as_view()),
    path("collections/<uuid:pk>/entries/", Entries.as_view()),
    path("collections/<uuid:pk>/entries/<uuid:item_pk>/", EntryDetail.as_view()),
    path("search/", SearchEntries.as_view()),
]

urlpatterns += [
    path("collections/<uuid:pk>/add/", AddMember.as_view()),
    path("collections/<uuid:pk>/remove/", RemoveMember.as_view()),
]
