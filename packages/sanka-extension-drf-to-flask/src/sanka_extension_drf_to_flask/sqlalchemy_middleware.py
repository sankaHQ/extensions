# SPDX-License-Identifier: Apache-2.0
"""Native equivalents of the captured stock HTTP middleware, in source order."""

from __future__ import annotations

import importlib
import re
from typing import Any

from flask import Flask, Response, g, request
from werkzeug.exceptions import RequestEntityTooLarge, SecurityError
from werkzeug.wsgi import get_host


def install_middleware(app: Flask, contract: dict[str, Any], patterns: Any) -> None:
    facts = contract["middleware"]
    security = contract["http_security"]
    exemptions = [re.compile(pattern) for pattern in facts["redirect_exempt"]]
    session_runtime = (
        importlib.import_module(__package__ + ".sqlalchemy_sessions")
        if any(
            view.get("auth", {}).get("kind") == "session"
            for view in contract.get("views", {}).values()
        )
        else None
    )
    session_context = (
        session_runtime.configure_runtime(app, contract) if session_runtime is not None else None
    )
    app.extensions["sanka_session_runtime"] = session_context

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_error: RequestEntityTooLarge) -> Response:
        return Response(facts["bad_request"], 400, content_type="text/html; charset=utf-8")

    def redirect(location: str) -> Response:
        # Django permanent redirects have an empty body, unlike Flask's HTML redirect helper.
        return Response("", 301, {"Location": location}, content_type="text/html; charset=utf-8")

    def full_path(slash: bool = False) -> str:
        path = request.path + ("/" if slash else "")
        path = path.replace("//", "/%2F", 1) if path.startswith("//") else path
        return path + ("?" + request.query_string.decode("latin1") if request.query_string else "")

    def valid_path(path: str, append: bool = False) -> bool:
        return any(
            pattern.fullmatch(path.lstrip("/"))
            and (not append or metadata.get("append_slash", True))
            for pattern, metadata in patterns
        )

    def checked_host() -> str:
        hosts = security["allowed_hosts"]
        return get_host(request.environ, trusted_hosts=None if "*" in hosts else hosts)

    @app.before_request
    def source_request() -> Response | None:
        g.sanka_middleware = []
        for middleware in facts["order"]:
            kind = middleware.rsplit(".", 1)[-1]
            g.sanka_middleware.append(kind)
            try:
                if kind == "CsrfViewMiddleware" and session_runtime is not None:
                    session_runtime.prepare_csrf(session_context)
                elif kind == "CommonMiddleware":
                    checked_host()
                elif kind == "SecurityMiddleware" and (
                    security["ssl_redirect"]
                    and not request.is_secure
                    and not any(pattern.search(request.path.lstrip("/")) for pattern in exemptions)
                ):
                    return redirect(
                        "https://" + (facts["redirect_host"] or checked_host()) + full_path()
                    )
            except SecurityError:
                # Django converts an exception outside the failing middleware's response hook.
                g.sanka_middleware.pop()
                return Response(facts["bad_request"], 400, content_type="text/html; charset=utf-8")
        return None

    @app.after_request
    def source_response(response: Response) -> Response:
        response.automatically_set_content_length = False
        response.headers.pop("Content-Length", None)
        for kind in reversed(getattr(g, "sanka_middleware", [])):
            if kind == "SessionMiddleware" and session_runtime is not None:
                try:
                    response = session_runtime.save_response(session_context, response)
                except session_runtime.SessionInterrupted:
                    response = Response(
                        facts["bad_request"], 400, content_type="text/html; charset=utf-8"
                    )
                    response.automatically_set_content_length = False
                    response.headers.pop("Content-Length", None)
            elif kind == "CsrfViewMiddleware" and session_runtime is not None:
                response = session_runtime.save_csrf(session_context, response)
            elif kind == "CommonMiddleware":
                if (
                    response.status_code == 404
                    and security["append_slash"]
                    and not request.path.endswith("/")
                    and not valid_path(request.path)
                    and valid_path(request.path + "/", append=True)
                ):
                    response = redirect(full_path(slash=True))
                    response.automatically_set_content_length = False
                response.headers.setdefault("Content-Length", str(len(response.get_data())))
            elif kind == "XFrameOptionsMiddleware":
                response.headers.setdefault("X-Frame-Options", security["x_frame_options"].upper())
            elif kind == "SecurityMiddleware":
                if security["content_type_nosniff"]:
                    response.headers.setdefault("X-Content-Type-Options", "nosniff")
                if facts["referrer_policy"]:
                    response.headers.setdefault("Referrer-Policy", facts["referrer_policy"])
                if security["cross_origin_opener_policy"]:
                    response.headers.setdefault(
                        "Cross-Origin-Opener-Policy", security["cross_origin_opener_policy"]
                    )
                if security["hsts_seconds"] and request.is_secure:
                    hsts = "max-age=" + str(security["hsts_seconds"])
                    if security["hsts_include_subdomains"]:
                        hsts += "; includeSubDomains"
                    if security["hsts_preload"]:
                        hsts += "; preload"
                    response.headers.setdefault("Strict-Transport-Security", hsts)
        return response
