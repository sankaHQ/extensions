# SPDX-License-Identifier: Apache-2.0
"""Source-compatible form decoding for generated Flask apps retaining Django.

Use parse_form(request), not request.form/request.files, when preserving Django
multipart behavior. This includes legacy boundary-token truncation: compatibility
is not a claim that the source preserves every uploaded byte.
"""

import codecs
from typing import Any


class FormError(Exception):
    def __init__(self, detail: str, status: int = 400) -> None:
        self.detail, self.status = detail, status


def parse_form(
    request: Any,
    media_types: tuple[str, ...] = (
        "multipart/form-data",
        "application/x-www-form-urlencoded",
    ),
) -> Any:
    from django.conf import settings  # type: ignore[import-untyped]
    from django.core.files.uploadhandler import load_handler  # type: ignore[import-untyped]
    from django.http import QueryDict  # type: ignore[import-untyped]
    from django.http.multipartparser import (  # type: ignore[import-untyped]
        MultiPartParser,
        MultiPartParserError,
    )
    from flask import after_this_request, g

    if hasattr(g, "_sanka_form"):
        return g._sanka_form
    encoding = request.mimetype_params.get("charset") or settings.DEFAULT_CHARSET
    try:
        codecs.lookup(encoding)
    except LookupError:
        encoding = settings.DEFAULT_CHARSET
    if not request.content_length:
        data = QueryDict("", mutable=True)
    elif request.mimetype not in media_types:
        raise FormError(f'Unsupported media type "{request.content_type or ""}" in request.', 415)
    elif request.mimetype == "application/x-www-form-urlencoded":
        data = QueryDict(request.get_data(), encoding=encoding)
    else:
        handlers = [load_handler(name) for name in settings.FILE_UPLOAD_HANDLERS]
        try:
            fields, files = MultiPartParser(
                request.environ,
                request.stream,
                handlers,
                encoding,
            ).parse()
        except MultiPartParserError as error:
            raise FormError("Multipart form parse error - " + str(error)) from error
        data = fields.copy()
        data.update(files)

        @after_this_request
        def close_uploads(response: Any) -> Any:
            for _, uploads in files.lists():
                for upload in uploads:
                    response.call_on_close(upload.close)
            return response

    g._sanka_form = data
    return data
