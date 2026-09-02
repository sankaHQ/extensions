from __future__ import annotations

import hashlib
import json
from typing import Any

from rest_framework import filters, status
from rest_framework.pagination import CursorPagination
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from records.models import Record
from records.serializers import RecordSerializer


class StableCursorPagination(CursorPagination):
    page_size = 2
    ordering = ("-posted_at", "-id")


class StableOrderingFilter(filters.OrderingFilter):
    def get_ordering(self, request: Request, queryset: Any, view: Any) -> list[str] | None:
        ordering = super().get_ordering(request, queryset, view)
        if ordering and "id" not in {term.removeprefix("-") for term in ordering}:
            ordering.append("-id" if ordering[-1].startswith("-") else "id")
        return ordering


class RecordViewSet(ModelViewSet):
    queryset = Record.objects.all()
    serializer_class = RecordSerializer
    pagination_class = StableCursorPagination
    filter_backends = [filters.SearchFilter, StableOrderingFilter]
    search_fields = ["label", "category"]
    ordering_fields = ["amount", "posted_at", "label"]

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        response = super().retrieve(request, *args, **kwargs)
        return self._conditional_response(request, response)

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        response = super().update(request, *args, **kwargs)
        return self._conditional_response(request, response)

    def _conditional_response(self, request: Request, response: Response) -> Response:
        etag = self._etag(response.data)
        headers = {
            "Cache-Control": "private, max-age=0",
            "ETag": etag,
            "Vary": "Accept",
        }
        if self._etag_matches(request.headers.get("If-None-Match", ""), etag):
            return Response(status=status.HTTP_304_NOT_MODIFIED, headers=headers)
        for name, value in headers.items():
            response[name] = value
        return response

    @staticmethod
    def _etag(payload: Any) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f'"{hashlib.sha256(canonical).hexdigest()}"'

    @staticmethod
    def _etag_matches(raw_header: str, etag: str) -> bool:
        if not raw_header:
            return False
        return any(candidate.strip() in {"*", etag} for candidate in raw_header.split(","))
