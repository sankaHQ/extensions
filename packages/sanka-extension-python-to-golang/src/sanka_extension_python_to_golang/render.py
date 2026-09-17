# SPDX-License-Identifier: Apache-2.0
"""Pinned framework adapters for captured JSON GET contracts."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from .capture import canonical
from .database import render_database
from .models import go_name

MODULES = {
    "fiber": "github.com/gofiber/fiber/v3",
    "chi": "github.com/go-chi/chi/v5",
    "mux": "github.com/gorilla/mux",
    "gin": "github.com/gin-gonic/gin",
}


def render(captured: dict[str, Any]) -> dict[str, str]:
    if captured["gaps"]:
        raise ValueError("resolve source capture gaps before generation")
    target = captured["configuration"]["target_framework"]
    module = MODULES[target]
    registrations = []
    helpers = []
    has_reads = any("read" in route for route in captured["routes"])
    for index, route in enumerate(captured["routes"]):
        path = canonical(route["path"])
        if "read" in route:
            model = next(
                item for item in captured["models"] if item["name"] == route["read"]["model"]
            )
            helpers.append(_read_helper(index, route["read"], model))
            raw_query = {
                "fiber": "string(c.Request().URI().QueryString())",
                "gin": "c.Request.URL.RawQuery",
                "chi": "r.URL.RawQuery",
                "mux": "r.URL.RawQuery",
            }[target]
            query_argument = ", " + raw_query if "filter" in route["read"] else ""
            if target == "fiber":
                registrations.append(f"""app.Get({path}, func(c fiber.Ctx) error {{
        body, err := readRows{index}(c.Context(), pool{query_argument})
        c.Set("Content-Type", "application/json")
        if err != nil {{ return c.Status(500).Send([]byte(`{{"error":"database read failed"}}`)) }}
        return c.Send(body)
    }})""")
            elif target == "gin":
                registrations.append(f"""app.GET({path}, func(c *gin.Context) {{
        body, err := readRows{index}(c.Request.Context(), pool{query_argument})
        if err != nil {{
            c.Data(500, "application/json", []byte(`{{"error":"database read failed"}}`))
            return
        }}
        c.Data(200, "application/json", body)
    }})""")
            else:
                method = (
                    f'app.MethodFunc("GET", {path},'
                    if target == "chi"
                    else f"app.HandleFunc({path},"
                )
                suffix = ")" if target == "chi" else ').Methods("GET")'
                registrations.append(f"""{method} func(w http.ResponseWriter, r *http.Request) {{
        body, err := readRows{index}(r.Context(), pool{query_argument})
        w.Header().Set("Content-Type", "application/json")
        if err != nil {{
            w.WriteHeader(500)
            _, _ = w.Write([]byte(`{{"error":"database read failed"}}`))
            return
        }}
        _, _ = w.Write(body)
    }}{suffix}""")
            continue
        body = canonical(canonical(route["body"]))
        if target == "fiber":
            registrations.append(f"""app.Get({path}, func(c fiber.Ctx) error {{
        c.Set("Content-Type", "application/json")
        return c.Send([]byte({body}))
    }})""")
        elif target == "gin":
            registrations.append(f"""app.GET({path}, func(c *gin.Context) {{
        c.Data(200, "application/json", []byte({body}))
    }})""")
        else:
            method = (
                f'app.MethodFunc("GET", {path},' if target == "chi" else f"app.HandleFunc({path},"
            )
            suffix = ")" if target == "chi" else ').Methods("GET")'
            registrations.append(f"""{method} func(w http.ResponseWriter, r *http.Request) {{
        w.Header().Set("Content-Type", "application/json")
        _, _ = w.Write([]byte({body}))
    }}{suffix}""")
    setup = {
        "fiber": "app := fiber.New(fiber.Config{DisableHeadAutoRegister: true})",
        "chi": "app := chi.NewRouter()",
        "mux": "app := mux.NewRouter()",
        "gin": (
            "app := gin.New()\n    app.RedirectTrailingSlash = false\n"
            "    app.RedirectFixedPath = false"
        ),
    }[target]
    return_type = "*fiber.App" if target == "fiber" else "http.Handler"
    http_import = "" if target == "fiber" else '"net/http"\n'
    database_import = (
        '"context"; "encoding/json"; "github.com/jackc/pgx/v5/pgxpool"' if has_reads else ""
    )
    arguments = "pool *pgxpool.Pool" if has_reads else ""
    guard = 'if pool == nil { panic("NewApp requires a database pool") }' if has_reads else ""
    source = f'''// SPDX-License-Identifier: Apache-2.0
// Generated experimental endpoint contract; not a complete backend migration.
package backend
import (
    {http_import}"{module}"
    {database_import}
)
func NewApp({arguments}) {return_type} {{
    {guard}
    {setup}
    {chr(10).join(registrations)}
    return app
}}
{chr(10).join(helpers)}
'''
    # A library-shaped app avoids inventing deployment settings during endpoint qualification.
    lock_name = target + (
        "-postgresql" if captured["configuration"]["database_layer"] == "pgx" else ""
    )
    lock = files("sanka_extension_python_to_golang").joinpath("locks", lock_name)
    result = {
        "app.go": source,
        "go.mod": lock.joinpath("go.mod").read_text(),
        "go.sum": lock.joinpath("go.sum").read_text(),
        "contract.json": canonical(captured) + "\n",
    }

    if captured["configuration"]["database_layer"] == "pgx":
        result.update(render_database(captured))
    if any("filter" in route.get("read", {}) for route in captured["routes"]):
        first = str(captured["configuration"]["source_framework"] == "flask").lower()
        result["query.go"] = QUERY_SOURCE.replace("QUERY_FIRST", first)
    return result


def _read_helper(index: int, read: dict[str, Any], model: dict[str, Any]) -> str:
    columns = ", ".join('"' + field["name"] + '"' for field in model["fields"])
    filtered = read.get("filter")
    where = f' WHERE "{filtered["field"]}" = $2' if filtered else ""
    query = (
        f'SELECT {columns} FROM "{model["table"]}"{where} ORDER BY "{read["order_by"]}" LIMIT $1'
    )
    signature = ", rawQuery string" if filtered else ""
    parameter = (
        f", queryValue(rawQuery, {json.dumps(filtered['parameter'], ensure_ascii=False)}, "
        f"{json.dumps(filtered['default'], ensure_ascii=False)})"
        if filtered
        else ""
    )
    destinations = ", ".join("&item." + go_name(field["name"]) for field in model["fields"])
    arguments = "ctx context.Context, pool *pgxpool.Pool" + signature
    return f"""func readRows{index}({arguments}) ([]byte, error) {{
    rows, err := pool.Query(ctx, {canonical(query)}, {read["limit"]}{parameter})
    if err != nil {{ return nil, err }}
    defer rows.Close()
    items := make([]{model["name"]}, 0)
    for rows.Next() {{
        var item {model["name"]}
        if err := rows.Scan({destinations}); err != nil {{ return nil, err }}
        items = append(items, item)
    }}
    if err := rows.Err(); err != nil {{ return nil, err }}
    return json.Marshal(items)
}}
"""


QUERY_SOURCE = """// SPDX-License-Identifier: Apache-2.0
package backend

import ("encoding/hex"; "strings"; "unicode/utf8")

// Flask selects the first value; DRF and FastAPI select the last.
const queryFirstValue = QUERY_FIRST
const queryPreserveInvalid = QUERY_FIRST

func queryValue(raw, key, fallback string) string {
    value := fallback
    for _, pair := range strings.Split(raw, "&") {
        if pair == "" { continue }
        name, item, _ := strings.Cut(pair, "=")
        if queryDecode(name) == key {
            value = queryDecode(item)
            if queryFirstValue { return value }
        }
    }
    return value
}

func queryDecode(raw string) string {
    var decoded strings.Builder
    for i := 0; i < len(raw); i++ {
        if raw[i] == '+' { decoded.WriteByte(' '); continue }
        if raw[i] == '%' && i+2 < len(raw) {
            if value, err := hex.DecodeString(raw[i+1:i+3]); err == nil {
                decoded.WriteByte(value[0]); i += 2; continue
            }
        }
        decoded.WriteByte(raw[i])
    }
    remaining := decoded.String()
    var result strings.Builder
    for len(remaining) > 0 {
        r, size := utf8.DecodeRuneInString(remaining)
        if r != utf8.RuneError || size != 1 {
            result.WriteRune(r); remaining = remaining[size:]; continue
        }
        // Consume a valid UTF-8 prefix as one decoding error, as Python does.
        width := 1
        first := remaining[0]
        if first >= 0xC2 && first <= 0xDF { width = 2 }
        if first >= 0xE0 && first <= 0xEF { width = 3 }
        if first >= 0xF0 && first <= 0xF4 { width = 4 }
        for size < width && size < len(remaining) {
            low, high := byte(0x80), byte(0xBF)
            if size == 1 {
                if first == 0xE0 { low = 0xA0 }
                if first == 0xED { high = 0x9F }
                if first == 0xF0 { low = 0x90 }
                if first == 0xF4 { high = 0x8F }
            }
            if remaining[size] < low || remaining[size] > high { break }
            size++
        }
        if queryPreserveInvalid {
            // Werkzeug preserves undecodable bytes using uppercase percent escapes.
            const digits = "0123456789ABCDEF"
            for _, b := range []byte(remaining[:size]) {
                result.WriteByte('%')
                result.WriteByte(digits[b>>4]); result.WriteByte(digits[b&15])
            }
        } else {
            result.WriteRune(utf8.RuneError)
        }
        remaining = remaining[size:]
    }
    return result.String()
}
"""
