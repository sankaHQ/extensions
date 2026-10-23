# SPDX-License-Identifier: Apache-2.0
"""Generated HTTP boundaries reject unsafe encodings and redirect targets."""

import asyncio
import json
import re
import sys
import types

import pytest


@pytest.fixture
def native_runtime(tmp_path, monkeypatch):
    from sanka_extension_drf_to_fastapi.native_async import _RUNTIME

    (tmp_path / "sanka-manifest.json").write_text(
        json.dumps({"http_security": {"append_slash": True}})
    )
    monkeypatch.setitem(sys.modules, "sanka_store", types.ModuleType("sanka_store"))
    runtime = {"__file__": str(tmp_path / "sanka_native.py")}
    exec(compile(_RUNTIME, runtime["__file__"], "exec"), runtime)
    return runtime


@pytest.mark.parametrize(
    "decoded, expected",
    [
        ("/\\evil.example/item", "/%5Cevil.example/item/"),
        ("//evil.example/item", "/%2Fevil.example/item/"),
        ("/item\rX-Injected: yes", "/item%0DX-Injected:%20yes/"),
        ("/日本語%name", "/%E6%97%A5%E6%9C%AC%E8%AA%9E%25name/"),
        ("/item;a=b", "/item%3Ba%3Db/"),
    ],
)
def test_append_slash_encodes_decoded_paths(native_runtime, decoded, expected):
    from fastapi import FastAPI, Request
    from flask import Flask

    from sanka_extension_drf_to_flask.sqlalchemy_middleware import install_middleware

    app = FastAPI()
    app.add_api_route("/{item:path}/", lambda: None)
    request = Request(
        {
            "type": "http",
            "path": decoded,
            "query_string": b"next=%2F%2Fevil",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "method": "GET",
            "root_path": "",
            "app": app,
        }
    )
    response = native_runtime["not_found_response"](request, "missing")
    assert response.status_code == 301
    assert response.headers["location"] == expected + "?next=%2F%2Fevil"

    flask = Flask(__name__)
    install_middleware(
        flask,
        {
            "middleware": {
                "order": ["django.middleware.common.CommonMiddleware"],
                "redirect_exempt": [],
            },
            "http_security": {"allowed_hosts": ["testserver"], "append_slash": True},
        },
        [(re.compile(r".*/$"), {})],
    )
    # Feed the decoded WSGI path directly, including double slashes that URL clients normalize.
    with flask.test_request_context(
        "/",
        base_url="http://testserver",
        environ_overrides={
            "PATH_INFO": decoded.encode("utf-8").decode("latin1"),
            "QUERY_STRING": "next=%2F%2Fevil",
        },
    ):
        response = flask.full_dispatch_request()
    assert response.status_code == 301
    assert response.headers["Location"] == expected + "?next=%2F%2Fevil"


@pytest.mark.parametrize("charset", ["punycode", "idna", "unicode_escape", "unknown-codec"])
def test_request_decoding_rejects_transform_codecs(charset):
    from sanka_extension_drf_to_flask.native_runtime import request_encoding

    with pytest.raises(ValueError):
        request_encoding(charset)


@pytest.mark.parametrize("charset, raw", [("utf8", b"caf\xc3\xa9"), ("iso-8859-1", b"caf\xe9")])
def test_request_decoding_keeps_supported_text(charset, raw):
    from sanka_extension_drf_to_flask.native_runtime import request_encoding

    assert raw.decode(request_encoding(charset)) == "café"


def test_two_hop_url_membership_fails_closed_before_database(native_runtime):
    from fastapi import Request

    from sanka_extension_drf_to_flask.sqlalchemy_access import permission_error

    access = {
        "permission": {
            "kind": "membership",
            "superuser": True,
            "parent_param": "pk",
            "relation": {"foreign_key": "collection_id"},
        }
    }
    request = Request({"type": "http", "path_params": {"pk": "1"}})
    with pytest.raises(ValueError):
        asyncio.run(
            native_runtime["_access_permission"]({"access": access}, request, {}, None, "GET")
        )
    with pytest.raises(ValueError):
        permission_error(None, {}, access, None, {"pk": "1"}, None, {})
