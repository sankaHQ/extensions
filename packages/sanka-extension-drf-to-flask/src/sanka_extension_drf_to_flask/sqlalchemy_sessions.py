# SPDX-License-Identifier: Apache-2.0
"""Capture and execute the bounded stock Django database-session contract."""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import inspect
import json
import os
import secrets
import string
import time
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit


def _cookie_settings(settings: Any, prefix: str) -> dict[str, Any]:
    values = {
        "name": getattr(settings, prefix + "_NAME"),
        "age": getattr(settings, prefix + "_AGE"),
        "domain": getattr(settings, prefix + "_DOMAIN"),
        "path": getattr(settings, prefix + "_PATH"),
        "secure": getattr(settings, prefix + "_SECURE"),
        "httponly": getattr(settings, prefix + "_HTTPONLY"),
        "samesite": getattr(settings, prefix + "_SAMESITE"),
    }
    if (
        type(values["name"]) is not str
        or not values["name"]
        or type(values["age"]) is not int
        or values["age"] <= 0
        or (values["domain"] is not None and type(values["domain"]) is not str)
        or type(values["path"]) is not str
        or not values["path"].startswith("/")
        or type(values["secure"]) is not bool
        or type(values["httponly"]) is not bool
        or values["samesite"] not in {None, "Lax", "Strict", "None", False}
    ):
        raise ValueError(prefix + " settings are outside the stock cookie contract")
    return values


def capture_session_auth(view_class: type[Any]) -> dict[str, Any]:
    """Return portable facts for one exact stock SessionAuthentication view."""
    authentication = importlib.import_module("rest_framework.authentication")
    permissions = importlib.import_module("rest_framework.permissions")
    settings = importlib.import_module("django.conf").settings
    if tuple(view_class.authentication_classes) != (authentication.SessionAuthentication,):
        raise ValueError("session authentication must be the only authenticator")
    if tuple(view_class.permission_classes) != (permissions.IsAuthenticated,):
        raise ValueError("session authentication currently requires exact IsAuthenticated")
    middleware = list(settings.MIDDLEWARE)
    session_middleware = "django.contrib.sessions.middleware.SessionMiddleware"
    auth_middleware = "django.contrib.auth.middleware.AuthenticationMiddleware"
    csrf_middleware = "django.middleware.csrf.CsrfViewMiddleware"
    required_middleware = (session_middleware, auth_middleware, csrf_middleware)
    if any(middleware.count(item) != 1 for item in required_middleware):
        raise ValueError("stock session, authentication and CSRF middleware are required once")
    if middleware.index(session_middleware) > middleware.index(auth_middleware):
        raise ValueError("SessionMiddleware must precede AuthenticationMiddleware")
    if settings.AUTHENTICATION_BACKENDS != ["django.contrib.auth.backends.ModelBackend"]:
        raise ValueError("session authentication requires the stock ModelBackend")
    if settings.SESSION_ENGINE != "django.contrib.sessions.backends.db":
        raise ValueError("only the Django database session backend is supported")
    if settings.SESSION_SERIALIZER != "django.contrib.sessions.serializers.JSONSerializer":
        raise ValueError("only Django's JSON session serializer is supported")
    if not settings.USE_TZ:
        raise ValueError("database sessions require timezone-aware expiry")
    if settings.CSRF_USE_SESSIONS:
        raise ValueError("CSRF_USE_SESSIONS is outside the bounded cookie contract")

    trusted_origins = list(settings.CSRF_TRUSTED_ORIGINS)
    for origin in trusted_origins:
        parsed = urlsplit(origin)
        if (
            type(origin) is not str
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("CSRF_TRUSTED_ORIGINS contains an unsupported origin")

    base_user = importlib.import_module("django.contrib.auth.base_user").AbstractBaseUser
    user = importlib.import_module("django.contrib.auth").get_user_model()
    deferred = importlib.import_module("django.db.models.query_utils").DeferredAttribute
    if (
        inspect.getattr_static(user, "get_session_auth_hash")
        is not inspect.getattr_static(base_user, "get_session_auth_hash")
        or inspect.getattr_static(user, "get_session_auth_fallback_hash")
        is not inspect.getattr_static(base_user, "get_session_auth_fallback_hash")
        or inspect.getattr_static(user, "_get_session_auth_hash")
        is not inspect.getattr_static(base_user, "_get_session_auth_hash")
        or type(inspect.getattr_static(user, "password")) is not deferred
    ):
        raise ValueError("custom session auth hash behavior is unsupported")
    models = importlib.import_module("django.db.models")
    manager = user._default_manager
    if inspect.getattr_static(type(manager), "get") is not inspect.getattr_static(
        models.Manager, "get"
    ) or "get" in vars(manager):
        raise ValueError("custom user manager lookup behavior is unsupported")
    from sanka_code_migration.drf.access_contracts import user_identity

    user_fact: dict[str, Any] = dict(user_identity())
    user_fact["password"] = str(user._meta.get_field("password").column)
    connection = importlib.import_module("django.db").connection
    pk_min, pk_max = connection.ops.integer_field_range(user._meta.pk.get_internal_type())
    if pk_min is None or pk_max is None:
        raise ValueError("session user primary-key bounds are unavailable")
    user_fact["pk_min"] = int(pk_min)
    user_fact["pk_max"] = int(pk_max)
    session_model = importlib.import_module("django.contrib.sessions.models").Session
    session_fact = {
        "table": str(session_model._meta.db_table),
        **{
            name: str(session_model._meta.get_field(name).column)
            for name in ("session_key", "session_data", "expire_date")
        },
        **{
            "cookie_" + name: value
            for name, value in _cookie_settings(settings, "SESSION_COOKIE").items()
        },
        "save_every_request": settings.SESSION_SAVE_EVERY_REQUEST,
        "expire_at_browser_close": settings.SESSION_EXPIRE_AT_BROWSER_CLOSE,
    }
    if (
        type(session_fact["save_every_request"]) is not bool
        or type(session_fact["expire_at_browser_close"]) is not bool
    ):
        raise ValueError("session expiry settings must be Boolean")
    csrf = importlib.import_module("django.middleware.csrf")
    exceptions = importlib.import_module("rest_framework.exceptions")
    csrf_fact = {
        **{
            "cookie_" + name: value
            for name, value in _cookie_settings(settings, "CSRF_COOKIE").items()
        },
        "header_name": str(settings.CSRF_HEADER_NAME),
        "allowed_hosts": list(settings.ALLOWED_HOSTS),
        "host_pattern": importlib.import_module("django.http.request").host_validation_re.pattern,
        "trusted_origins": trusted_origins,
        "messages": {
            "no_cookie": str(csrf.REASON_NO_CSRF_COOKIE),
            "token_missing": str(csrf.REASON_CSRF_TOKEN_MISSING),
            "bad_origin": str(csrf.REASON_BAD_ORIGIN),
            "no_referer": str(csrf.REASON_NO_REFERER),
            "malformed_referer": str(csrf.REASON_MALFORMED_REFERER),
            "insecure_referer": str(csrf.REASON_INSECURE_REFERER),
            "bad_referer": str(csrf.REASON_BAD_REFERER),
        },
    }
    if not csrf_fact["header_name"].startswith("HTTP_"):
        raise ValueError("CSRF_HEADER_NAME must use Django's HTTP_ request header form")
    return {
        "kind": "session",
        "require_authenticated": True,
        "backend": "django.contrib.auth.backends.ModelBackend",
        "secret_env": "SANKA_DJANGO_SECRET_KEY",
        "fallbacks_env": "SANKA_DJANGO_SECRET_KEY_FALLBACKS",
        "fallback_count": len(settings.SECRET_KEY_FALLBACKS),
        "user": user_fact,
        "session": session_fact,
        "csrf": csrf_fact,
        "messages": {
            "no_credentials": str(exceptions.NotAuthenticated.default_detail),
            "csrf_prefix": "CSRF Failed: ",
        },
    }


_SESSION_KEY = "_auth_user_id"
_BACKEND_KEY = "_auth_user_backend"
_HASH_KEY = "_auth_user_hash"
_ALPHABET = string.ascii_lowercase + string.digits
_CSRF_CHARS = string.ascii_letters + string.digits


class SessionInterrupted(RuntimeError):
    """The loaded database session disappeared before its response save."""


def configure_runtime(app: Any, contract: dict[str, Any]) -> dict[str, Any] | None:
    """Read target-only secrets and prepare lazy request session access."""
    contracts = [
        view["auth"]
        for view in contract["views"].values()
        if view.get("auth", {}).get("kind") == "session"
    ]
    if not contracts:
        return None
    facts = contracts[0]
    if any(value != facts for value in contracts[1:]):
        raise RuntimeError("generated session contracts disagree")
    secret = os.environ.get(facts["secret_env"])
    if not secret:
        raise RuntimeError(facts["secret_env"] + " must be set")
    try:
        fallbacks = json.loads(os.environ.get(facts["fallbacks_env"], "[]"))
    except json.JSONDecodeError as error:
        raise RuntimeError(facts["fallbacks_env"] + " must be a JSON string array") from error
    if (
        type(fallbacks) is not list
        or len(fallbacks) != facts["fallback_count"]
        or any(type(value) is not str or not value for value in fallbacks)
    ):
        raise RuntimeError(facts["fallbacks_env"] + " does not match the captured fallback count")
    return {
        "facts": facts,
        "secret": secret,
        "fallbacks": fallbacks,
        "tables": app.extensions["sanka_tables"],
        "sessions": app.extensions["sanka_sessions"],
    }


def _signature(value: str, secret: str, salt: str, algorithm: str = "sha256") -> str:
    digest = getattr(hashlib, algorithm)
    key = digest((salt + "signer" + secret).encode()).digest()
    value_hash = hmac.new(key, value.encode(), digest).digest()
    return base64.urlsafe_b64encode(value_hash).decode().rstrip("=")


def _decode(value: str, secrets: list[str]) -> tuple[dict[str, Any], int | None]:
    try:
        signed, signature = value.rsplit(":", 1)
        payload, _timestamp = signed.rsplit(":", 1)
        timestamp_digits = _timestamp.removeprefix("-")
        if not timestamp_digits or any(
            character not in string.digits + string.ascii_letters for character in timestamp_digits
        ):
            return {}, None
        matched = next(
            (
                index
                for index, secret in enumerate(secrets)
                if hmac.compare_digest(
                    signature,
                    _signature(
                        payload + ":" + _timestamp,
                        secret,
                        "django.contrib.sessions.SessionStore",
                    ),
                )
            ),
            None,
        )
        if matched is None:
            return {}, None
        compressed = payload.startswith(".")
        encoded = payload[1:] if compressed else payload
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if compressed:
            raw = zlib.decompress(raw)
        decoded = json.loads(raw.decode("latin1"))
        return (decoded, matched) if type(decoded) is dict else ({}, matched)
    except (ValueError, TypeError, UnicodeError, zlib.error, json.JSONDecodeError):
        return {}, None


def _base62(value: int) -> str:
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    encoded = ""
    while value:
        value, remainder = divmod(value, 62)
        encoded = chars[remainder] + encoded
    return encoded


def _encode(data: dict[str, Any], secret: str) -> str:
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode("latin1")
    compressed = zlib.compress(raw)
    use_compressed = len(compressed) < len(raw) - 1
    payload = base64.urlsafe_b64encode(compressed if use_compressed else raw).decode().rstrip("=")
    if use_compressed:
        payload = "." + payload
    signed = payload + ":" + _base62(int(time.time()))
    return signed + ":" + _signature(signed, secret, "django.contrib.sessions.SessionStore")


def _vary_cookie(response: Any) -> None:
    response.vary.add("Cookie")


def _state(context: dict[str, Any]) -> dict[str, Any]:
    from flask import g, request

    current = getattr(g, "sanka_session_state", None)
    if current is not None:
        return cast(dict[str, Any], current)
    facts = context["facts"]["session"]
    incoming = request.cookies.get(facts["cookie_name"])
    key = incoming if incoming and len(incoming) >= 8 else None
    data: dict[str, Any] = {}
    matched: int | None = None
    if key is not None:
        import sqlalchemy as sa

        table = context["tables"][facts["table"]]
        with context["sessions"]() as session:
            row = (
                session.execute(
                    sa.select(table).where(
                        table.c[facts["session_key"]] == key,
                        table.c[facts["expire_date"]] > datetime.now(UTC),
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            key = None
        else:
            data, matched = _decode(
                row[facts["session_data"]], [context["secret"], *context["fallbacks"]]
            )
    current = {
        "incoming": incoming,
        "key": key,
        "data": data,
        "matched_secret": matched,
        "accessed": True,
        "modified": False,
    }
    g.sanka_session_state = current
    return current


def _auth_hash(password: str, secret: str) -> str:
    key = hashlib.sha256(
        ("django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash" + secret).encode()
    ).digest()
    return hmac.new(key, password.encode(), hashlib.sha256).hexdigest()


def _delete(context: dict[str, Any], key: str | None) -> None:
    if key is None:
        return
    import sqlalchemy as sa

    facts = context["facts"]["session"]
    table = context["tables"][facts["table"]]
    with context["sessions"]() as session:
        session.execute(sa.delete(table).where(table.c[facts["session_key"]] == key))
        session.commit()


def _new_key(context: dict[str, Any]) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(32))


def _expiry(data: dict[str, Any], facts: dict[str, Any]) -> tuple[datetime, datetime, int, bool]:
    modification = datetime.now(UTC)
    value = data.get("_session_expiry")
    browser_close = facts["expire_at_browser_close"] if value is None else value == 0
    if isinstance(value, str):
        expiry_date = datetime.fromisoformat(value)
        delta = expiry_date - modification
        age = delta.days * 86_400 + delta.seconds
    else:
        age = facts["cookie_age"] if not value else int(value)
        expiry_date = modification + timedelta(seconds=age)
    return modification, expiry_date, age, browser_close


def _session_values(context: dict[str, Any], key: str, data: dict[str, Any]) -> dict[str, Any]:
    facts = context["facts"]["session"]
    _, expiry_date, _, _ = _expiry(data, facts)
    return {
        facts["session_key"]: key,
        facts["session_data"]: _encode(data, context["secret"]),
        facts["expire_date"]: expiry_date,
    }


def _cycle(context: dict[str, Any], state: dict[str, Any]) -> None:
    import sqlalchemy as sa

    facts = context["facts"]["session"]
    table = context["tables"][facts["table"]]
    old_key = state["key"]
    while True:
        key = _new_key(context)
        with context["sessions"]() as session:
            try:
                session.execute(
                    sa.insert(table).values(**_session_values(context, key, state["data"]))
                )
                if old_key is not None:
                    session.execute(
                        sa.delete(table).where(table.c[facts["session_key"]] == old_key)
                    )
                session.commit()
            except sa.exc.IntegrityError:
                session.rollback()
                continue
        state["key"] = key
        return


def _load_user(context: dict[str, Any], state: dict[str, Any]) -> Any:
    import sqlalchemy as sa

    facts = context["facts"]
    data = state["data"]
    if data.get(_BACKEND_KEY) != facts["backend"] or _SESSION_KEY not in data:
        return None
    user_fact = facts["user"]
    users = context["tables"][user_fact["table"]]
    try:
        user_id = int(data[_SESSION_KEY])
    except (TypeError, ValueError):
        return None
    if not user_fact["pk_min"] <= user_id <= user_fact["pk_max"]:
        return None
    with context["sessions"]() as session:
        user = (
            session.execute(sa.select(users).where(users.c[user_fact["pk"]] == user_id))
            .mappings()
            .first()
        )
    if user is None or not user[user_fact["active"]]:
        return None
    current_hash = _auth_hash(str(user[user_fact["password"]]), context["secret"])
    session_hash = str(data.get(_HASH_KEY, "")).encode()
    if hmac.compare_digest(session_hash, current_hash.encode()):
        return user
    fallback_hashes = [
        _auth_hash(str(user[user_fact["password"]]), key) for key in context["fallbacks"]
    ]
    if any(hmac.compare_digest(session_hash, value.encode()) for value in fallback_hashes):
        _cycle(context, state)
        state["data"][_HASH_KEY] = current_hash
        state["modified"] = True
        return user
    _delete(context, state["key"])
    state["key"] = None
    state["data"] = {}
    state["modified"] = True
    return None


def _csrf_header_name(value: str) -> str:
    return "-".join(part.capitalize() for part in value.removeprefix("HTTP_").split("_"))


def _unmask(token: str) -> str:
    if len(token) == 32:
        return token
    first = token[:32]
    second = token[32:]
    return "".join(
        _CSRF_CHARS[(_CSRF_CHARS.index(y) - _CSRF_CHARS.index(x)) % len(_CSRF_CHARS)]
        for x, y in zip(first, second, strict=True)
    )


def _same_domain(host: str, pattern: str) -> bool:
    pattern = pattern.lower()
    return bool(pattern) and (
        host == pattern
        or (pattern.startswith(".") and (host.endswith(pattern) or host == pattern[1:]))
    )


def _trusted(origin: str, expected: str | None, trusted: list[str]) -> bool:
    if origin == expected or origin in {value for value in trusted if "*" not in value}:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return any(
        parsed.scheme == urlsplit(value).scheme
        and _same_domain(parsed.netloc, urlsplit(value).netloc.lstrip("*"))
        for value in trusted
        if "*" in value
    )


def _checked_csrf_host(csrf: dict[str, Any]) -> str | None:
    import re

    from flask import request

    host = request.environ.get("HTTP_HOST")
    if host is None:
        host = request.environ["SERVER_NAME"]
        port = str(request.environ["SERVER_PORT"])
        if port != ("443" if request.is_secure else "80"):
            host += ":" + port
    match = re.fullmatch(csrf["host_pattern"], host.lower())
    if match is None:
        return None
    domain = match.group(1).removesuffix(".")
    if domain and any(
        pattern == "*" or _same_domain(domain, pattern) for pattern in csrf["allowed_hosts"]
    ):
        return str(host)
    return None


def prepare_csrf(context: dict[str, Any] | None) -> None:
    """Preserve stock middleware repair of an invalid incoming CSRF cookie."""
    if context is None:
        return
    from flask import g, request

    cookie = request.cookies.get(context["facts"]["csrf"]["cookie_name"])
    if cookie is not None and (
        len(cookie) not in {32, 64} or any(char not in _CSRF_CHARS for char in cookie)
    ):
        g.sanka_csrf_replacement = "".join(secrets.choice(_CSRF_CHARS) for _ in range(32))


def preserve_cookie_domain(response: Any, name: str, domain: str | None) -> None:
    # Werkzeug strips leading dots; Django preserves the configured cookie domain.
    if domain is None:
        return
    from http.cookies import SimpleCookie

    headers = response.headers.getlist("Set-Cookie")
    cookie = SimpleCookie(headers[-1])
    cookie[name]["domain"] = domain
    headers[-1] = cookie.output(header="").strip()
    response.headers.setlist("Set-Cookie", headers)


def save_csrf(context: dict[str, Any] | None, response: Any) -> Any:
    if context is None:
        return response
    from flask import g

    replacement = getattr(g, "sanka_csrf_replacement", None)
    if replacement is not None:
        facts = context["facts"]["csrf"]
        response.set_cookie(
            facts["cookie_name"],
            replacement,
            max_age=facts["cookie_age"],
            domain=facts["cookie_domain"],
            path=facts["cookie_path"],
            secure=facts["cookie_secure"],
            httponly=facts["cookie_httponly"],
            samesite=facts["cookie_samesite"] or None,
        )
        preserve_cookie_domain(response, facts["cookie_name"], facts["cookie_domain"])
        _vary_cookie(response)
        del g.sanka_csrf_replacement
    return response


def _csrf_failure(context: dict[str, Any]) -> str | None:
    from flask import request

    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return None
    csrf = context["facts"]["csrf"]
    messages = cast(dict[str, str], csrf["messages"])
    host = _checked_csrf_host(csrf)
    expected_origin = request.scheme + "://" + host if host is not None else None
    if "Origin" in request.headers:
        origin = request.headers["Origin"]
        if not _trusted(origin, expected_origin, csrf["trusted_origins"]):
            return messages["bad_origin"] % origin
    elif request.is_secure:
        referer_value = request.headers.get("Referer")
        if referer_value is None:
            return messages["no_referer"]
        try:
            parsed = urlsplit(referer_value)
        except ValueError:
            return messages["malformed_referer"]
        if not parsed.scheme or not parsed.netloc:
            return messages["malformed_referer"]
        if parsed.scheme != "https":
            return messages["insecure_referer"]
        trusted = any(
            _same_domain(parsed.netloc, urlsplit(origin).netloc.lstrip("*"))
            for origin in csrf["trusted_origins"]
        )
        good_referer = csrf["cookie_domain"]
        if good_referer is None:
            good_referer = host
        else:
            port = str(request.environ["SERVER_PORT"])
            if port not in {"80", "443"}:
                good_referer += ":" + port
        if not trusted and (good_referer is None or not _same_domain(parsed.netloc, good_referer)):
            return messages["bad_referer"] % parsed.geturl()
    cookie = request.cookies.get(csrf["cookie_name"])
    if cookie is None:
        return messages["no_cookie"]
    if len(cookie) not in {32, 64}:
        return "CSRF cookie has incorrect length."
    if any(character not in _CSRF_CHARS for character in cookie):
        return "CSRF cookie has invalid characters."
    header = _csrf_header_name(csrf["header_name"])
    if header not in request.headers:
        return messages["token_missing"]
    token = request.headers[header]
    token_source = repr(header) + " HTTP header"
    if len(token) not in {32, 64}:
        return f"CSRF token from the {token_source} has incorrect length."
    if any(character not in _CSRF_CHARS for character in token):
        return f"CSRF token from the {token_source} has invalid characters."
    if not hmac.compare_digest(_unmask(cookie), _unmask(token)):
        return f"CSRF token from the {token_source} incorrect."
    return None


def authenticate(context: dict[str, Any] | None, auth: dict[str, Any]) -> tuple[Any, Any]:
    """Return the authenticated row or a DRF-shaped 403 response."""
    from flask import jsonify

    if context is None or context["facts"] != auth:
        raise RuntimeError("session authentication runtime was not configured")
    state = _state(context)
    user = _load_user(context, state)
    if user is None:
        response = jsonify({"detail": auth["messages"]["no_credentials"]})
        response.status_code = 403
        return None, response
    reason = _csrf_failure(context)
    if reason is not None:
        response = jsonify({"detail": auth["messages"]["csrf_prefix"] + reason})
        response.status_code = 403
        return None, response
    return user, None


def save_response(context: dict[str, Any] | None, response: Any) -> Any:
    """Apply the stock database SessionMiddleware response side effects."""
    if context is None:
        return response
    from flask import g

    state = getattr(g, "sanka_session_state", None)
    if state is None:
        return response
    facts = context["facts"]["session"]
    _vary_cookie(response)
    if state["incoming"] and state["key"] is None and not state["data"]:
        samesite = facts["cookie_samesite"] or None
        secure = facts["cookie_name"].startswith(("__Secure-", "__Host-")) or (
            isinstance(samesite, str) and samesite.lower() == "none"
        )
        response.delete_cookie(
            facts["cookie_name"],
            path=facts["cookie_path"],
            domain=facts["cookie_domain"],
            secure=secure,
            samesite=samesite,
        )
        preserve_cookie_domain(response, facts["cookie_name"], facts["cookie_domain"])
        return response
    if not (state["modified"] or facts["save_every_request"]) or (
        state["key"] is None and not state["data"]
    ):
        return response
    if response.status_code >= 500:
        return response
    import sqlalchemy as sa

    key = state["key"]
    table = context["tables"][facts["table"]]
    _, expires, max_age, browser_close = _expiry(state["data"], facts)
    if key is None:
        _cycle(context, state)
        key = state["key"]
    values = _session_values(context, key, state["data"])
    with context["sessions"]() as session:
        updated = session.execute(
            sa.update(table).where(table.c[facts["session_key"]] == key).values(**values)
        )
        if updated.rowcount == 0:
            session.rollback()
            raise SessionInterrupted("the session was deleted before the request completed")
        session.commit()
    response.set_cookie(
        facts["cookie_name"],
        key,
        max_age=None if browser_close else max_age,
        expires=None if browser_close else expires,
        path=facts["cookie_path"],
        domain=facts["cookie_domain"],
        secure=facts["cookie_secure"],
        httponly=facts["cookie_httponly"],
        samesite=facts["cookie_samesite"] or None,
    )
    preserve_cookie_domain(response, facts["cookie_name"], facts["cookie_domain"])
    return response
