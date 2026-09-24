# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Explicit security recipes; source code and credentials are never evaluated."""

from __future__ import annotations

import ast
import copy
from typing import Any

HEADER_NAMES = {
    "cache-control",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "strict-transport-security",
}


def _access_body(framework: str, kind: str = "bearer-read-write") -> list[ast.stmt]:
    def denied(status: int, message: str) -> str:
        headers = ', headers={"WWW-Authenticate": "Bearer"}' if status == 401 else ""
        if framework == "fastapi":
            return f'return JSONResponse(status_code={status}, content={{"detail": "{message}"}}{headers})'
        if framework == "drf":
            return f'return JsonResponse({{"error": "{message}"}}, status={status}{headers})'
        suffix = ', {"WWW-Authenticate": "Bearer"}' if status == 401 else ""
        return f'return jsonify({{"error": "{message}"}}), {status}{suffix}'

    if kind == "jwt-hs256-roles":
        from .jwt_security import ACCESS_BODY

        return ast.parse(
            ACCESS_BODY.replace("UNAVAILABLE", denied(503, "authentication unavailable"))
            .replace("UNAUTHENTICATED", denied(401, "not authenticated"))
            .replace("FORBIDDEN", denied(403, "permission denied"))
        ).body
    return ast.parse(f"""token = request.headers.get("Authorization", "").strip(" \\t")
reader = environ.get("AUTH_READ_TOKEN", "")
writer = environ.get("AUTH_WRITE_TOKEN", "")
if not reader or not writer or not reader.isascii() or not writer.isascii() or reader == writer:
    {denied(503, "authentication unavailable")}
is_reader = compare_digest(token.encode("utf-8"), ("Bearer " + reader).encode("utf-8"))
is_writer = compare_digest(token.encode("utf-8"), ("Bearer " + writer).encode("utf-8"))
if not is_reader and not is_writer:
    {denied(401, "not authenticated")}
if request.method not in ("GET", "HEAD", "OPTIONS") and not is_writer:
    {denied(403, "permission denied")}
""").body


def _same(body: list[ast.stmt], expected: list[ast.stmt]) -> bool:
    return ast.dump(ast.Module(body=body, type_ignores=[])) == ast.dump(
        ast.Module(body=expected, type_ignores=[])
    )


def _signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef, arguments: str, *, asynchronous: bool = False
) -> bool:
    return (
        isinstance(node, ast.AsyncFunctionDef) == asynchronous
        and not node.returns
        and not node.type_params
        and ast.unparse(node.args) == arguments
    )


def _headers(body: list[ast.stmt], framework: str) -> dict[str, str]:
    result = {}
    for item in body:
        if not (
            isinstance(item, ast.Assign)
            and len(item.targets) == 1
            and isinstance(item.targets[0], ast.Subscript)
        ):
            raise ValueError("security response hooks require literal header assignments")
        target = item.targets[0]
        name, value = ast.literal_eval(target.slice), ast.literal_eval(item.value)
        receiver = "response" if framework == "drf" else "response.headers"
        if (
            ast.unparse(target.value) != receiver
            or type(name) is not str
            or name.lower() not in HEADER_NAMES
            or type(value) is not str
            or len(value) > 256
            or any(ord(c) < 32 or ord(c) > 126 for c in value)
        ):
            raise ValueError("unsupported security response header")
        result[name.lower()] = value
    return result


def normalize_security(tree: ast.Module, framework: str) -> tuple[ast.Module, dict[str, Any]]:
    from .native_security import normalize_native_security

    native = normalize_native_security(tree, framework)
    if native is not None:
        return native
    tree = copy.deepcopy(tree)
    consumed: list[ast.stmt] = []
    hooks: list[tuple[int, dict[str, str]]] = []
    guard_index = None
    guard_class = None
    kind = "bearer-read-write"
    required = {("os", "environ")}

    def access_kind(body: list[ast.stmt]) -> str | None:
        for candidate in ("bearer-read-write", "jwt-hs256-roles"):
            if _same(body, _access_body(framework, candidate)):
                return candidate
        return None

    imports = {
        (node.module, alias.name): i
        for i, node in enumerate(tree.body)
        if isinstance(node, ast.ImportFrom) and not node.level
        for alias in node.names
        if alias.asname is None
    }
    for index, node in enumerate(tree.body):
        guard = False
        if framework == "drf" and isinstance(node, ast.ClassDef):
            if (
                node.bases
                or node.keywords
                or node.decorator_list
                or node.type_params
                or len(node.body) != 3
            ):
                continue
            init, request, response = node.body
            if not all(isinstance(n, ast.FunctionDef) and not n.decorator_list for n in node.body):
                continue
            assert (
                isinstance(init, ast.FunctionDef)
                and isinstance(request, ast.FunctionDef)
                and isinstance(response, ast.FunctionDef)
            )
            if (
                init.name != "__init__"
                or request.name != "process_request"
                or response.name != "process_response"
                or not _signature(init, "self, get_response")
                or not _signature(request, "self, request")
                or not _signature(response, "self, request, response")
                or not _same(init.body, ast.parse("self.get_response = get_response").body)
                or not access_kind(request.body)
                or ast.unparse(response.body[-1]) != "return response"
            ):
                continue
            hooks.append((index, _headers(response.body[:-1], framework)))
            guard, guard_class = True, node.name
            kind = access_kind(request.body) or kind
            required |= {
                ("django.http", "JsonResponse"),
                ("django.utils.decorators", "decorator_from_middleware"),
            }
        elif (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and len(node.decorator_list) == 1
        ):
            decorator = ast.unparse(node.decorator_list[0])
            if framework == "fastapi" and decorator == "app.middleware('http')":
                if not _signature(node, "request: Request, call_next", asynchronous=True):
                    raise ValueError("unsupported HTTP middleware signature")
                if ast.unparse(node.body[-1]) == "return await call_next(request)" and access_kind(
                    node.body[:-1]
                ):
                    guard = True
                    kind = access_kind(node.body[:-1]) or kind
                    required |= {("fastapi", "Request"), ("fastapi.responses", "JSONResponse")}
                elif (
                    ast.unparse(node.body[0]) == "response = await call_next(request)"
                    and ast.unparse(node.body[-1]) == "return response"
                ):
                    hooks.append((index, _headers(node.body[1:-1], framework)))
                    required.add(("fastapi", "Request"))
                else:
                    raise ValueError("HTTP middleware is outside the qualified security recipes")
            elif framework == "flask" and decorator == "app.before_request":
                if not _signature(node, "") or not access_kind(node.body):
                    raise ValueError("before_request is outside the qualified access recipe")
                guard = True
                kind = access_kind(node.body) or kind
                required |= {("flask", "request"), ("flask", "jsonify")}
            elif framework == "flask" and decorator == "app.after_request":
                if (
                    not _signature(node, "response")
                    or ast.unparse(node.body[-1]) != "return response"
                ):
                    raise ValueError("unsupported after_request signature")
                hooks.append((index, _headers(node.body[:-1], framework)))
            else:
                continue
        else:
            continue
        if guard:
            if guard_index is not None:
                raise ValueError("multiple access policies require additional capture")
            guard_index = index
        consumed.append(node)
        # Consumed declarations must not erase invalid definition-time annotations.
        if framework == "fastapi" and imports.get(("fastapi", "Request"), len(tree.body)) >= index:
            raise ValueError("Request must be imported before middleware definitions")
    if not consumed:
        return tree, {}
    if kind == "jwt-hs256-roles":
        from .application import _bindings

        if {"len", "set", "type", "str", "int"}.intersection(
            name for node in tree.body for name in _bindings(node)
        ):
            raise ValueError("JWT middleware builtin dependencies must not be shadowed")
    required |= (
        {
            ("jwt", "decode"),
            ("jwt", "get_unverified_header"),
            ("jwt", "InvalidTokenError"),
            ("re", "fullmatch"),
            ("time", "time"),
        }
        if kind == "jwt-hs256-roles"
        else {("hmac", "compare_digest")}
    )
    if guard_index is None or not required <= imports.keys():
        raise ValueError("security hooks require the explicit bearer access policy and imports")
    # DRF decorators wrap the entire APIView, before its parsing and validation.
    if framework == "drf":
        count = 0
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                wanted = f"decorator_from_middleware({guard_class})"
                if not node.decorator_list or ast.unparse(node.decorator_list[0]) != wanted:
                    raise ValueError(
                        "every DRF view must explicitly wrap the captured access middleware"
                    )
                if tree.body.index(node) <= guard_index or imports.get(
                    ("django.utils.decorators", "decorator_from_middleware"), len(tree.body)
                ) >= tree.body.index(node):
                    raise ValueError("DRF middleware must be defined before view decoration")
                node.decorator_list.pop(0)
                count += 1
        if not count:
            raise ValueError("unused DRF access middleware")
    success: dict[str, str] = {}
    denied: dict[str, str] = {}
    for index, headers in reversed(hooks) if framework == "flask" else hooks:
        success.update(headers)
        if framework == "flask" or (framework == "fastapi" and index > guard_index):
            denied.update(headers)
    tree.body = [node for node in tree.body if node not in consumed]
    referenced = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            node.names = [
                a
                for a in node.names
                if (node.module, a.name) not in required or (a.asname or a.name) in referenced
            ]
    tree.body = [node for node in tree.body if not isinstance(node, ast.ImportFrom) or node.names]
    return tree, {
        "kind": kind,
        "scope": "views" if framework == "drf" else "application",
        "success_headers": success,
        "denied_headers": denied,
    }


def render_security(captured: dict[str, Any]) -> str:
    import json
    import re

    policy = captured["security"]
    target = captured["configuration"]["target_framework"]
    paths = "|".join(
        re.sub(r":[A-Za-z_][A-Za-z_0-9]*", "[0-9]+", re.escape(r["path"]))
        for r in captured["routes"]
    )
    scope_import = '"regexp";' if policy["scope"] == "views" else ""
    scope = (
        f"var securityPaths = regexp.MustCompile({json.dumps('^(?:' + paths + ')$')})"
        if scope_import
        else ""
    )
    scope_check = "if !securityPaths.MatchString(path) { return 404 }" if scope else ""

    def headers(values: dict[str, str]) -> str:
        return (
            "map[string]string{"
            + ",".join(json.dumps(k) + ":" + json.dumps(v) for k, v in sorted(values.items()))
            + "}"
        )

    key = policy.get(
        "error_key",
        "detail" if captured["configuration"]["source_framework"] == "fastapi" else "error",
    )
    common = f'''// SPDX-License-Identifier: Apache-2.0
package backend
import ("crypto/subtle"; "os"; "strings"; {scope_import} ADAPTER_IMPORT)
{scope}
func accessStatus(token, method, path string) int {{
    {scope_check}
    token = strings.Trim(token, " \t")
    reader, writer := os.Getenv("AUTH_READ_TOKEN"), os.Getenv("AUTH_WRITE_TOKEN")
    if reader == "" || writer == "" || !asciiCredential(reader) || !asciiCredential(writer) || reader == writer {{ return 503 }}
    isReader := subtle.ConstantTimeCompare([]byte(token), []byte("Bearer " + reader)) == 1
    isWriter := subtle.ConstantTimeCompare([]byte(token), []byte("Bearer " + writer)) == 1
    if !isReader && !isWriter {{ return 401 }}
    if method != "GET" && method != "HEAD" && method != "OPTIONS" && !isWriter {{ return 403 }}
    return 0
}}
func asciiCredential(value string) bool {{
    for i := 0; i < len(value); i++ {{ if value[i] > 127 {{ return false }} }}
    return true
}}
func accessHeaders(status int) map[string]string {{
    if status == 404 {{ return map[string]string{{}} }}
    if status != 0 {{ return {headers(policy["denied_headers"])} }}
    return {headers(policy["success_headers"])}
}}
func accessBody(status int) []byte {{
    switch status {{
    case 401: return []byte(`{{"{key}":"not authenticated"}}`)
    case 403: return []byte(`{{"{key}":"permission denied"}}`)
    case 404: return []byte(`{{"{key}":"not found"}}`)
    default: return []byte(`{{"{key}":"authentication unavailable"}}`)
    }}
}}
'''
    if target == "fiber":
        adapter_import = '"github.com/gofiber/fiber/v3"'
        adapter = """func securityMiddleware(c fiber.Ctx) error {
    status := accessStatus(c.Get("Authorization"), c.Method(), c.Path())
    for key,value := range accessHeaders(status) { c.Set(key,value) }
    if status != 0 {
        if status == 401 { c.Set("WWW-Authenticate","Bearer") }
        c.Set("Content-Type","application/json")
        return c.Status(status).Send(accessBody(status))
    }
    return c.Next()
}
"""
    elif target == "gin":
        adapter_import = '"github.com/gin-gonic/gin"'
        adapter = """func securityMiddleware(c *gin.Context) {
    status := accessStatus(c.GetHeader("Authorization"), c.Request.Method, c.Request.URL.Path)
    for key,value := range accessHeaders(status) { c.Header(key,value) }
    if status != 0 {
        if status == 401 { c.Header("WWW-Authenticate","Bearer") }
        c.Data(status,"application/json",accessBody(status)); c.Abort(); return
    }
    c.Next()
}
"""
    else:
        adapter_import = '"net/http"'
        adapter = """func securityMiddleware(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        status := accessStatus(r.Header.Get("Authorization"), r.Method, r.URL.Path)
        for key,value := range accessHeaders(status) { w.Header().Set(key,value) }
        if status != 0 {
            if status == 401 { w.Header().Set("WWW-Authenticate","Bearer") }
            w.Header().Set("Content-Type","application/json")
            w.WriteHeader(status); _, _ = w.Write(accessBody(status)); return
        }
        next.ServeHTTP(w,r)
    })
}
"""
    if captured["configuration"]["source_framework"] == "flask":
        # WSGI combines repeated fields; selecting only the first weakens the policy.
        adapter = (
            adapter.replace(
                'c.Get("Authorization")', 'strings.Join(c.GetReqHeaders()["Authorization"], ", ")'
            )
            .replace(
                'c.GetHeader("Authorization")',
                'strings.Join(c.Request.Header.Values("Authorization"), ", ")',
            )
            .replace(
                'r.Header.Get("Authorization")',
                'strings.Join(r.Header.Values("Authorization"), ", ")',
            )
        )
    if policy["kind"] == "jwt-hs256-roles":
        from .jwt_security import GO_ACCESS

        if not policy.get("native"):
            start, end = common.index("func accessStatus("), common.index("func accessHeaders(")
            common = common[:start] + GO_ACCESS.replace("SCOPE_CHECK", scope_check) + common[end:]
        common = common.replace(
            '"crypto/subtle";',
            '\n"encoding/json"; "encoding/base64"; "unicode/utf8"; "strconv"; "time"; "github.com/golang-jwt/jwt/v5";',
        )
        if not scope_import:
            common = common.replace('"os";', '"os"; "regexp";')
    if policy.get("native"):
        common = common.replace('"encoding/json";', '"encoding/json"; "context";')
        start, end = common.index("func accessStatus("), common.index("func accessHeaders(")
        from .jwt_security import go_access_principal

        authenticate = go_access_principal(scope_check)
        common = common[:start] + authenticate + common[end:]
        common += """
type principalContextKey struct{}
func identityResponse(ctx context.Context, fields map[string]string) ([]byte, int) {
    principal, ok := ctx.Value(principalContextKey{}).(map[string]string)
    if !ok { return accessBody(503),503 }
    values := map[string]string{}
    for name, claim := range fields { values[name] = principal[claim] }
    body, err := json.Marshal(values)
    if err != nil { return accessBody(503),503 }
    return body,200
}
"""
        adapter = adapter.replace(
            "status := accessStatus(", "principal, status := accessPrincipal("
        )
        adapter = (
            adapter.replace(
                "return c.Next()",
                "c.SetContext(context.WithValue(c.Context(), principalContextKey{}, principal))\n    return c.Next()",
            )
            if target == "fiber"
            else adapter
        )
        if target == "gin":
            adapter = adapter.replace(
                "    c.Next()",
                "    c.Request = c.Request.WithContext(context.WithValue(c.Request.Context(), principalContextKey{}, principal))\n    c.Next()",
            )
        if target in {"chi", "mux"}:
            adapter = adapter.replace(
                "next.ServeHTTP(w,r)",
                "next.ServeHTTP(w,r.WithContext(context.WithValue(r.Context(), principalContextKey{}, principal)))",
            )
    return common.replace("ADAPTER_IMPORT", adapter_import) + adapter


# These are synthetic fixtures, never defaults in a generated application.
REPLAY_ENV = {"AUTH_READ_TOKEN": "sanka-replay-reader", "AUTH_WRITE_TOKEN": "sanka-replay-writer"}


def security_cases(
    cases: list[dict[str, Any]],
    kind: str = "bearer-read-write",
    routes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Exercise every supplied request as writer, unauthenticated and reader."""
    result = []
    for index, case in enumerate(cases):
        headers = case.get("headers", {})
        if any(key.lower() == "authorization" for key in headers):
            raise ValueError(
                "security replay supplies synthetic Authorization; omit it from scenarios"
            )
        roles: tuple[tuple[str, str, int], ...] = (
            ("writer", "Bearer " + REPLAY_ENV["AUTH_WRITE_TOKEN"], case["expected_status"]),
            ("missing", "", 401),
            ("invalid", "Bearer invalid-replay-token", 401),
            (
                "reader",
                "Bearer " + REPLAY_ENV["AUTH_READ_TOKEN"],
                case["expected_status"] if case["method"] in ("GET", "HEAD", "OPTIONS") else 403,
            ),
        )
        if kind == "jwt-hs256-roles":
            from .jwt_security import replay_roles

            roles = replay_roles(case, first=index == 0)
        bodies: dict[str, Any] = {}
        if routes and kind == "jwt-hs256-roles":
            from .row_security import scoped_replay_roles

            roles, bodies = scoped_replay_roles(case, roles, routes)
        for role, token, status in roles:
            result.append(
                dict(
                    case | ({"body": bodies[role]} if role in bodies else {}),
                    id=f"security.{index}.{role}",
                    expected_status=status,
                    headers=headers | ({"authorization": token} if token else {}),
                )
            )
    return result


def header_names(captured: dict[str, Any]) -> list[str]:
    policy = captured.get("security")
    return (
        sorted({"www-authenticate", *policy["success_headers"], *policy["denied_headers"]})
        if policy
        else []
    )


def security_environment(captured: dict[str, Any]) -> dict[str, str]:
    import json

    from .jwt_security import REPLAY_JWT_ENV

    environment = (
        REPLAY_JWT_ENV
        if captured.get("security", {}).get("kind") == "jwt-hs256-roles"
        else REPLAY_ENV
    )
    return (
        environment | {"SANKA_GO_REPLAY_HEADERS": json.dumps(header_names(captured))}
        if captured.get("security")
        else {"SANKA_GO_REPLAY_HEADERS": "[]"}
    )


def header_probe(probe: str, captured: dict[str, Any]) -> str:
    """Add the extension sidecar without changing shared HTTP observation v1."""
    import json

    names = header_names(captured)
    if not names:
        return probe
    native_headers = (
        "response.Header"
        if captured["configuration"]["target_framework"] == "fiber"
        else "response.Header()"
    )
    # Both runners expose the native response as response.
    probe = probe.replace(
        "observed := []map[string]any{}",
        "observed := []map[string]any{}\n    observedHeaders := []map[string]string{}",
    )
    probe = probe.replace(
        "if len(body) >",
        """headers := map[string]string{}
        for _, key := range []string{NAMES} { headers[key] = NATIVE.Get(key) }
        observedHeaders = append(observedHeaders, headers)
        if len(body) >""".replace("NAMES", ",".join(json.dumps(name) for name in names)).replace(
            "NATIVE", native_headers
        ),
        1,
    )
    return probe.replace(
        'if err := os.WriteFile("sanka-observed.json",',
        """headerData, err := json.Marshal(map[string]any{"schema":"sanka.go-security-headers/v1","responses":observedHeaders}); if err != nil { t.Fatal(err) }
    if err := os.WriteFile("sanka-observed.headers.json",headerData,0600); err != nil { t.Fatal(err) }
    if err := os.WriteFile("sanka-observed.json",""",
    )


def compare_headers(
    candidate: Any, source: Any, captured: dict[str, Any], count: int
) -> dict[str, Any]:
    names = set(header_names(captured))
    for document in (candidate, source):
        if document is None:
            continue
        if (
            not isinstance(document, dict)
            or set(document) != {"schema", "responses"}
            or document["schema"] != "sanka.go-security-headers/v1"
            or not isinstance(document["responses"], list)
            or len(document["responses"]) != count
            or any(
                not isinstance(row, dict)
                or set(row) != names
                or any(type(value) is not str for value in row.values())
                for row in document["responses"]
            )
        ):
            raise ValueError("invalid security header observations")
    return {"candidate": candidate, "source": source, "ok": source is None or candidate == source}
