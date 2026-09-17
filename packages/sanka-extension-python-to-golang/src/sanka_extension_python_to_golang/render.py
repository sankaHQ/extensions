# SPDX-License-Identifier: Apache-2.0
"""Pinned framework adapters for captured JSON GET contracts."""

from __future__ import annotations

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
            if target == "fiber":
                registrations.append(f"""app.Get({path}, func(c fiber.Ctx) error {{
        body, err := readRows{index}(c.Context(), pool)
        c.Set("Content-Type", "application/json")
        if err != nil {{ return c.Status(500).Send([]byte(`{{"error":"database read failed"}}`)) }}
        return c.Send(body)
    }})""")
            elif target == "gin":
                registrations.append(f"""app.GET({path}, func(c *gin.Context) {{
        body, err := readRows{index}(c.Request.Context(), pool)
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
        body, err := readRows{index}(r.Context(), pool)
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
    return result


def _read_helper(index: int, read: dict[str, Any], model: dict[str, Any]) -> str:
    columns = ", ".join('"' + field["name"] + '"' for field in model["fields"])
    query = f'SELECT {columns} FROM "{model["table"]}" ORDER BY "{read["order_by"]}" LIMIT $1'
    destinations = ", ".join("&item." + go_name(field["name"]) for field in model["fields"])
    return f"""func readRows{index}(ctx context.Context, pool *pgxpool.Pool) ([]byte, error) {{
    rows, err := pool.Query(ctx, {canonical(query)}, {read["limit"]})
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
