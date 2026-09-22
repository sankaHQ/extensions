# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Pinned framework adapters for captured JSON contracts."""

from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import Any

from .capture import canonical
from .database import render_database
from .models import go_name
from .security import render_security

MODULES = {
    "fiber": "github.com/gofiber/fiber/v3",
    "chi": "github.com/go-chi/chi/v5",
    "mux": "github.com/gorilla/mux",
    "gin": "github.com/gin-gonic/gin",
}


def _runtime(target: str, database: bool, database_configured: bool) -> dict[str, str]:
    database_import = '"github.com/jackc/pgx/v5/pgxpool"' if database else ""
    database_field = "\n    databaseURL string" if database else ""
    database_config = (
        """
    result.databaseURL = os.Getenv("DATABASE_URL")
    if result.databaseURL == "" { return config{}, fmt.Errorf("DATABASE_URL is required") }
"""
        if database
        else ""
    )
    database_setup = (
        """
    poolConfig, err := pgxpool.ParseConfig(cfg.databaseURL)
    if err != nil { return fmt.Errorf("invalid DATABASE_URL") }
    startupCtx, cancel := context.WithTimeout(runCtx, 10*time.Second)
    defer cancel()
    pool, err := pgxpool.NewWithConfig(startupCtx, poolConfig)
    if err != nil { return fmt.Errorf("database unavailable") }
    defer pool.Close()
    if err := pool.Ping(startupCtx); err != nil { return fmt.Errorf("database unavailable") }
    app := backend.NewApp(pool)
"""
        if database
        else "    app := backend.NewApp()\n"
    )
    database_test_setup = 't.Setenv("DATABASE_URL", "postgresql://test/db")' if database else ""
    database_test = (
        """
    t.Setenv("DATABASE_URL", "")
    if _, err := loadConfig(); err == nil { t.Fatal("missing DATABASE_URL accepted") }
"""
        if database
        else ""
    )
    if target == "fiber":
        runtime_imports = '"github.com/gofiber/fiber/v3"'
        serve = """    return app.Listen(cfg.address, fiber.ListenConfig{
        DisableStartupMessage: true,
        GracefulContext: runCtx,
        ShutdownTimeout: 10*time.Second,
    })"""
    else:
        runtime_imports = '"errors"\n    "net/http"'
        serve = """    server := &http.Server{
        Addr: cfg.address,
        Handler: http.MaxBytesHandler(app, 1048576),
        ReadHeaderTimeout: 5*time.Second,
        ReadTimeout: 10*time.Second,
        WriteTimeout: 30*time.Second,
        IdleTimeout: 60*time.Second,
        MaxHeaderBytes: 1048576,
    }
    serveErr := make(chan error, 1)
    go func() { serveErr <- server.ListenAndServe() }()
    select {
    case err := <-serveErr:
        if errors.Is(err, http.ErrServerClosed) { return nil }
        return err
    case <-runCtx.Done():
        shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
        defer cancel()
        if err := server.Shutdown(shutdownCtx); err != nil { return err }
        err := <-serveErr
        if errors.Is(err, http.ErrServerClosed) { return nil }
        return err
    }"""
    main = f"""// SPDX-License-Identifier: Apache-2.0
package main

import (
    "context"
    "fmt"
    "os"
    "os/signal"
    "strconv"
    "syscall"
    "time"

    {runtime_imports}
    {database_import}
    backend "migrated.backend"
)

type config struct {{
    address string{database_field}
}}

func loadConfig() (config, error) {{
    port := os.Getenv("PORT")
    if port == "" {{ port = "8080" }}
    value, err := strconv.Atoi(port)
    if err != nil || value < 1 || value > 65535 {{
        return config{{}}, fmt.Errorf("PORT must be an integer between 1 and 65535")
    }}
    result := config{{address: ":" + port}}{database_config}
    return result, nil
}}

func run() error {{
    cfg, err := loadConfig()
    if err != nil {{ return err }}
    runCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
    defer stop()
{database_setup}{serve}
}}

func main() {{
    if err := run(); err != nil {{
        fmt.Fprintln(os.Stderr, err)
        os.Exit(1)
    }}
}}
"""
    test = f"""// SPDX-License-Identifier: Apache-2.0
package main

import "testing"

func TestLoadConfig(t *testing.T) {{
    t.Setenv("PORT", "")
    {database_test_setup}
    cfg, err := loadConfig()
    if err != nil || cfg.address != ":8080" {{ t.Fatal("default configuration failed") }}
    for _, port := range []string{{"0", "65536", "invalid"}} {{
        t.Setenv("PORT", port)
        if _, err := loadConfig(); err == nil {{ t.Fatal("invalid PORT accepted") }}
    }}
    t.Setenv("PORT", "8080")
    {database_test}
}}
"""
    environment = "PORT=8080\n" + ("DATABASE_URL=\n" if database_configured else "")
    return {"cmd/api/main.go": main, "cmd/api/main_test.go": test, ".env.example": environment}


def render(captured: dict[str, Any]) -> dict[str, str]:
    if captured["gaps"]:
        raise ValueError("resolve source capture gaps before generation")
    target = captured["configuration"]["target_framework"]
    has_writes = any("write" in route for route in captured["routes"])
    has_patch = any(
        route.get("write", {}).get("operation") == "patch" for route in captured["routes"]
    )
    has_replace = any(
        route.get("write", {}).get("operation") == "replace" for route in captured["routes"]
    )
    has_delete = any(
        route.get("write", {}).get("operation") == "delete" for route in captured["routes"]
    )
    has_body_writes = any(
        route.get("write", {}).get("operation") in {"create", "replace", "patch"}
        for route in captured["routes"]
    )
    module = MODULES[target]
    error_key = "detail" if captured["configuration"]["source_framework"] == "fastapi" else "error"
    registrations = []
    helpers = []
    write_models: set[tuple[str, str]] = set()
    has_pydantic = any(
        route.get("write", {}).get("validation", {}).get("kind") == "pydantic"
        for route in captured["routes"]
    )
    has_drf = any(
        route.get("write", {}).get("validation", {}).get("kind") == "drf"
        for route in captured["routes"]
    )
    has_reads = any("read" in route for route in captured["routes"])
    has_pagination = any("pagination" in route.get("read", {}) for route in captured["routes"])
    has_detail_reads = any("lookup" in route.get("read", {}) for route in captured["routes"])
    for index, route in enumerate(captured["routes"]):
        path = canonical(route["path"])
        if "write" in route:
            model = next(
                item for item in captured["models"] if item["name"] == route["write"]["model"]
            )
            validation_kind = route["write"].get("validation", {}).get("kind", "strict")
            decoder_key = (model["name"], validation_kind)
            if route["write"]["operation"] == "delete":
                helpers.append(_delete_helper(index, route["write"], model))
                registrations.append(
                    _delete_registration(
                        index,
                        route,
                        model,
                        target,
                        error_key,
                        captured["configuration"]["source_framework"],
                    )
                )
                continue
            if route["write"].get("transaction"):
                helpers.append(_transaction_helper(index, route["write"], captured["models"]))
            else:
                helpers.append(
                    _write_helper(index, route["write"], model, decoder_key not in write_models)
                )
                if not route["write"].get("constraints"):
                    write_models.add(decoder_key)
            method = route["method"].title()
            status = route["status"]
            lookup_field = None
            if route["write"]["operation"] in {"replace", "patch"}:
                lookup_field = next(
                    item for item in model["fields"] if item["name"] == route["write"]["lookup"]
                )
            argument = ", lookup" if lookup_field else ""
            invalid_status = (
                route["write"].get("validation", {}).get("error", {}).get("status", 400)
            )
            if target == "fiber":
                lookup = ""
                if lookup_field:
                    bits = "32" if lookup_field["go_type"] == "int32" else "64"
                    lookup = f"""rawID, parseErr := strconv.ParseInt(c.Params({canonical(lookup_field["name"])}), 10, {bits})
        if parseErr != nil {{ return c.Status(400).JSON(fiber.Map{{{canonical(error_key)}: "invalid lookup"}}) }}
        lookup := {lookup_field["go_type"]}(rawID)"""
                registrations.append(f"""app.{method}({path}, func(c fiber.Ctx) error {{
        {lookup}
        item, err := writeRow{index}(c.Context(), pool, c.Body(){argument})
        if err != nil {{
            if errors.Is(err, errInvalidWrite) {{
                return c.Status({invalid_status}).JSON(fiber.Map{{{canonical(error_key)}: "invalid request body"}})
            }}
            if errors.Is(err, pgx.ErrNoRows) {{
                return c.Status(404).JSON(fiber.Map{{{canonical(error_key)}: "not found"}})
            }}
            return c.Status(500).JSON(fiber.Map{{"error": "database write failed"}})
        }}
        return c.Status({status}).JSON(item)
    }})""")
            elif target == "gin":
                lookup = ""
                if lookup_field:
                    bits = "32" if lookup_field["go_type"] == "int32" else "64"
                    lookup = f"""rawID, parseErr := strconv.ParseInt(c.Param({canonical(lookup_field["name"])}), 10, {bits})
        if parseErr != nil {{ c.JSON(400, gin.H{{{canonical(error_key)}: "invalid lookup"}}); return }}
        lookup := {lookup_field["go_type"]}(rawID)"""
                registrations.append(f"""app.{method.upper()}({path}, func(c *gin.Context) {{
        {lookup}
        body, readErr := io.ReadAll(http.MaxBytesReader(c.Writer, c.Request.Body, 1048576))
        if readErr != nil {{
            status := 400
            var tooLarge *http.MaxBytesError
            if errors.As(readErr, &tooLarge) {{ status = 413 }}
            c.JSON(status, gin.H{{{canonical(error_key)}: "invalid request body"}})
            return
        }}
        item, err := writeRow{index}(c.Request.Context(), pool, body{argument})
        if err != nil {{
            if errors.Is(err, errInvalidWrite) {{ c.JSON({invalid_status}, gin.H{{{canonical(error_key)}: "invalid request body"}}); return }}
            if errors.Is(err, pgx.ErrNoRows) {{ c.JSON(404, gin.H{{{canonical(error_key)}: "not found"}}); return }}
            c.JSON(500, gin.H{{"error": "database write failed"}})
            return
        }}
        c.JSON({status}, item)
    }})""")
            else:
                registered_path = route["path"]
                lookup = ""
                if lookup_field:
                    registered_path = registered_path.replace(
                        f":{lookup_field['name']}", f"{{{lookup_field['name']}}}"
                    )
                    bits = "32" if lookup_field["go_type"] == "int32" else "64"
                    parameter = (
                        f"chi.URLParam(r, {canonical(lookup_field['name'])})"
                        if target == "chi"
                        else f"mux.Vars(r)[{canonical(lookup_field['name'])}]"
                    )
                    lookup = f"""rawID, parseErr := strconv.ParseInt({parameter}, 10, {bits})
        if parseErr != nil {{ writeResponse(w, 400, map[string]string{{{canonical(error_key)}: "invalid lookup"}}); return }}
        lookup := {lookup_field["go_type"]}(rawID)"""
                registration = (
                    f'app.MethodFunc("{route["method"]}", {canonical(registered_path)},'
                    if target == "chi"
                    else f"app.HandleFunc({canonical(registered_path)},"
                )
                suffix = ")" if target == "chi" else f').Methods("{route["method"]}")'
                registrations.append(f"""{registration} func(w http.ResponseWriter, r *http.Request) {{
        {lookup}
        body, readErr := io.ReadAll(http.MaxBytesReader(w, r.Body, 1048576))
        if readErr != nil {{
            status := 400
            var tooLarge *http.MaxBytesError
            if errors.As(readErr, &tooLarge) {{ status = 413 }}
            writeResponse(w, status, map[string]string{{{canonical(error_key)}: "invalid request body"}})
            return
        }}
        item, err := writeRow{index}(r.Context(), pool, body{argument})
        if err != nil {{
            if errors.Is(err, errInvalidWrite) {{ writeResponse(w, {invalid_status}, map[string]string{{{canonical(error_key)}: "invalid request body"}}); return }}
            if errors.Is(err, pgx.ErrNoRows) {{ writeResponse(w, 404, map[string]string{{{canonical(error_key)}: "not found"}}); return }}
            writeResponse(w, 500, map[string]string{{"error": "database write failed"}})
            return
        }}
        writeResponse(w, {status}, item)
    }}{suffix}""")
            continue
        if "read" in route:
            model = next(
                item for item in captured["models"] if item["name"] == route["read"]["model"]
            )
            if "lookup" in route["read"]:
                helper = _read_helper if route["read"].get("many") else _detail_read_helper
                helpers.append(helper(index, route["read"], model))
                registrations.append(
                    _detail_read_registration(index, route, model, target, error_key)
                )
                continue
            helpers.append(_read_helper(index, route["read"], model))
            raw_query = {
                "fiber": "string(c.Request().URI().QueryString())",
                "gin": "c.Request.URL.RawQuery",
                "chi": "r.URL.RawQuery",
                "mux": "r.URL.RawQuery",
            }[target]
            paginated = "pagination" in route["read"]
            query_argument = ", " + raw_query if "filter" in route["read"] or paginated else ""
            pagination_error = (
                {
                    "fiber": 'if errors.Is(err, errInvalidPage) { return c.Status(400).Send([]byte(`{"detail":"invalid pagination"}`)) }',
                    "gin": 'if errors.Is(err, errInvalidPage) { c.Data(400, "application/json", []byte(`{"detail":"invalid pagination"}`)); return }',
                    "chi": 'if errors.Is(err, errInvalidPage) { w.WriteHeader(400); _, _ = w.Write([]byte(`{"detail":"invalid pagination"}`)); return }',
                    "mux": 'if errors.Is(err, errInvalidPage) { w.WriteHeader(400); _, _ = w.Write([]byte(`{"detail":"invalid pagination"}`)); return }',
                }[target]
                if paginated
                else ""
            )
            if target == "fiber":
                registrations.append(f"""app.Get({path}, func(c fiber.Ctx) error {{
        body, err := readRows{index}(c.Context(), pool{query_argument})
        c.Set("Content-Type", "application/json")
        {pagination_error}
        if err != nil {{ return c.Status(500).Send([]byte(`{{"error":"database read failed"}}`)) }}
        return c.Send(body)
    }})""")
            elif target == "gin":
                registrations.append(f"""app.GET({path}, func(c *gin.Context) {{
        body, err := readRows{index}(c.Request.Context(), pool{query_argument})
        {pagination_error}
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
        {pagination_error}
        if err != nil {{
            w.WriteHeader(500)
            _, _ = w.Write([]byte(`{{"error":"database read failed"}}`))
            return
        }}
        _, _ = w.Write(body)
    }}{suffix}""")
            continue
        if "identity" in route:
            fields = (
                "map[string]string{"
                + ",".join(
                    canonical(k) + ":" + canonical(v) for k, v in sorted(route["identity"].items())
                )
                + "}"
            )
            if target == "fiber":
                registrations.append(f"""app.Get({path}, func(c fiber.Ctx) error {{
        body, status := identityResponse(c.Context(), {fields})
        c.Set("Content-Type", "application/json")
        return c.Status(status).Send(body)
    }})""")
            elif target == "gin":
                registrations.append(f"""app.GET({path}, func(c *gin.Context) {{
        body, status := identityResponse(c.Request.Context(), {fields})
        c.Data(status, "application/json", body)
    }})""")
            else:
                method = (
                    f'app.MethodFunc("GET", {path},'
                    if target == "chi"
                    else f"app.HandleFunc({path},"
                )
                suffix = ")" if target == "chi" else ').Methods("GET")'
                registrations.append(f"""{method} func(w http.ResponseWriter, r *http.Request) {{
        body, status := identityResponse(r.Context(), {fields})
        w.Header().Set("Content-Type", "application/json")
        w.WriteHeader(status)
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
    has_integrity = any(
        route.get("write", {}).get("integrity_conflict") for route in captured["routes"]
    )
    for index, route in enumerate(captured["routes"]):
        if not route.get("write", {}).get("integrity_conflict"):
            continue
        if target == "fiber":
            conflict = f'return c.Status(409).JSON(fiber.Map{{{canonical(error_key)}: "integrity conflict"}})'
        elif target == "gin":
            conflict = f'c.JSON(409, gin.H{{{canonical(error_key)}: "integrity conflict"}}); return'
        else:
            conflict = f'writeResponse(w, 409, map[string]string{{{canonical(error_key)}: "integrity conflict"}}); return'
        registrations[index] = registrations[index].replace(
            "if err != nil {",
            "if err != nil {\n            if isIntegrityConflict(err) { " + conflict + " }\n",
        )
    if any(
        step.get("operation") in {"replace", "patch"}
        for route in captured["routes"]
        for step in route.get("write", {}).get("transaction", [])
    ):
        helpers.append("""func transactionUpdateError(err error) error {
    if errors.Is(err, pgx.ErrNoRows) { return errors.New("transaction record disappeared during update") }
    return err
}
""")
    setup = {
        "fiber": (
            "app := fiber.New(fiber.Config{DisableHeadAutoRegister: true, BodyLimit: 1048576, "
            "ReadTimeout: 10*time.Second, WriteTimeout: 30*time.Second, "
            "IdleTimeout: 60*time.Second})"
        ),
        "chi": "app := chi.NewRouter()",
        "mux": "app := mux.NewRouter()",
        "gin": (
            "app := gin.New()\n    app.RedirectTrailingSlash = false\n"
            "    app.RedirectFixedPath = false"
        ),
    }[target]
    if captured.get("security") and target in {"fiber", "gin"}:
        setup += "\n    app.Use(securityMiddleware)"
    returned = (
        "securityMiddleware(app)"
        if captured.get("security") and target in {"chi", "mux"}
        else "app"
    )
    return_type = "*fiber.App" if target == "fiber" else "http.Handler"
    http_import = "" if target == "fiber" else '"net/http"\n'
    database = has_reads or has_writes
    imports = []
    if has_reads:
        imports.extend(['"context"', '"encoding/json"'])
    if has_detail_reads:
        imports.extend(['"errors"', '"strconv"', '"github.com/jackc/pgx/v5"'])
    if target == "fiber":
        imports.append('"time"')
    if has_writes:
        imports.extend(
            [
                '"context"',
                '"errors"',
                '"github.com/jackc/pgx/v5"',
            ]
        )
    if has_integrity:
        imports.append('"github.com/jackc/pgx/v5/pgconn"')
        helpers.append("""func isIntegrityConflict(err error) bool {
    var databaseError *pgconn.PgError
    return errors.As(err, &databaseError) && len(databaseError.Code) == 5 && databaseError.Code[:2] == "23"
}
""")
    if has_body_writes:
        imports.extend(['"bytes"', '"encoding/json"', '"io"'])
    if has_writes and target in {"chi", "mux"}:
        imports.append('"encoding/json"')
    if any(
        step.get("operation") == "patch"
        for route in captured["routes"]
        for step in route.get("write", {}).get("transaction", [])
    ):
        imports.extend(['"fmt"', '"strings"'])
    if has_patch:
        imports.extend(['"fmt"', '"strconv"', '"strings"'])
    elif has_replace or has_delete:
        imports.append('"strconv"')
    if has_pydantic or has_drf:
        imports.extend(['"math"', '"strconv"', '"strings"'])
    if has_pagination:
        imports.append('"errors"')
    if database:
        imports.append('"github.com/jackc/pgx/v5/pgxpool"')
    database_import = "; ".join(dict.fromkeys(imports))
    arguments = "pool *pgxpool.Pool" if database else ""
    guard = 'if pool == nil { panic("NewApp requires a database pool") }' if database else ""
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
    return {returned}
}}
{chr(10).join(helpers)}
'''
    if has_writes:
        source += '\nvar errInvalidWrite = errors.New("invalid write")\n'
        if has_pydantic:
            source += PYDANTIC_HELPERS
        if has_drf:
            source += DRF_HELPERS
        if target in {"chi", "mux"}:
            source += """
func writeResponse(w http.ResponseWriter, status int, payload any) {
    w.Header().Set("Content-Type", "application/json")
    w.WriteHeader(status)
    _ = json.NewEncoder(w).Encode(payload)
}
"""
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
    result.update(_runtime(target, database, captured["configuration"]["database_layer"] == "pgx"))
    if captured.get("security"):
        result["security.go"] = render_security(captured)
        if captured["security"]["kind"] == "jwt-hs256-roles":
            jwt_lock = files("sanka_extension_python_to_golang").joinpath("locks", "jwt")
            result["go.mod"] += "\n" + jwt_lock.joinpath("go.mod").read_text()
            result["go.sum"] = (
                "\n".join(
                    sorted(
                        set(result["go.sum"].splitlines())
                        | set(jwt_lock.joinpath("go.sum").read_text().splitlines())
                    )
                )
                + "\n"
            )
            result[".env.example"] += "AUTH_JWT_SECRET=\nAUTH_JWT_ISSUER=\nAUTH_JWT_AUDIENCE=\n"
        else:
            result[".env.example"] += "AUTH_READ_TOKEN=\nAUTH_WRITE_TOKEN=\n"
    if has_pagination:
        result["pagination.go"] = PAGINATION_SOURCE
    if has_pagination or any("filter" in route.get("read", {}) for route in captured["routes"]):
        first = str(captured["configuration"]["source_framework"] == "flask").lower()
        result["query.go"] = QUERY_SOURCE.replace("QUERY_FIRST", first)
    if any(route.get("read", route.get("write", {})).get("scope") for route in captured["routes"]):
        result["row_security.go"] = """// SPDX-License-Identifier: Apache-2.0
package backend
import ("context"; "errors")
var errScopeDenied = errors.New("permission denied")
func rowPrincipal(ctx context.Context) (map[string]string, error) {
    principal, ok := ctx.Value(principalContextKey{}).(map[string]string)
    if !ok || principal["sub"] == "" || principal["tenant"] == "" { return nil, errScopeDenied }
    return principal, nil
}
"""
        # Qualified write policies use the source's operation error envelope.
        app = result["app.go"]
        if target == "fiber":
            denial = f'if errors.Is(err, errScopeDenied) {{ return c.Status(403).JSON(fiber.Map{{{canonical(error_key)}: "permission denied"}}) }}'
        elif target == "gin":
            denial = f'if errors.Is(err, errScopeDenied) {{ c.JSON(403, gin.H{{{canonical(error_key)}: "permission denied"}}); return }}'
        else:
            denial = f'if errors.Is(err, errScopeDenied) {{ writeResponse(w, 403, map[string]string{{{canonical(error_key)}: "permission denied"}}); return }}'
        if has_writes:
            app = app.replace(
                "if errors.Is(err, errInvalidWrite)",
                denial + "\n            if errors.Is(err, errInvalidWrite)",
            )
        result["app.go"] = app
    return result


def _scope_parts(operation: dict[str, Any], start: int, failure: str) -> tuple[str, str, str]:
    scope = operation.get("scope", {})
    if not scope:
        return "", "", ""
    guard = f"principal, scopeErr := rowPrincipal(ctx)\n    if scopeErr != nil {{ {failure} }}"
    predicates = " AND ".join(f'"{field}" = ${number}' for number, field in enumerate(scope, start))
    arguments = ", " + ", ".join(f"principal[{canonical(claim)}]" for claim in scope.values())
    return guard, predicates, arguments


def _read_helper(index: int, read: dict[str, Any], model: dict[str, Any]) -> str:
    columns = ", ".join('"' + field["name"] + '"' for field in model["fields"])
    filtered = read.get("filter")
    pagination = read.get("pagination")
    related = next(
        (field for field in model["fields"] if field["name"] == read.get("lookup")), None
    )
    where = f' WHERE "{filtered["field"]}" = ${3 if pagination else 2}' if filtered else ""
    if related:
        where = f' WHERE "{related["name"]}" = $2'
    guard, predicate, scoped_values = _scope_parts(
        read, 1 + (2 if pagination else 1) + bool(filtered or related), "return nil, scopeErr"
    )
    if predicate:
        where += (" AND " if where else " WHERE ") + predicate
    query = (
        f'SELECT {columns} FROM "{model["table"]}"{where} ORDER BY "{read["order_by"]}" LIMIT $1'
    )
    if pagination:
        query += " OFFSET $2"
    signature = ", rawQuery string" if filtered or pagination else ""
    if related:
        signature = f", lookup {related['go_type']}"
    parameter = (
        f", queryValue(rawQuery, {json.dumps(filtered['parameter'], ensure_ascii=False)}, "
        f"{json.dumps(filtered['default'], ensure_ascii=False)})"
        if filtered
        else ""
    )
    if related:
        parameter = ", lookup"
    destinations = ", ".join("&item." + go_name(field["name"]) for field in model["fields"])
    arguments = "ctx context.Context, pool *pgxpool.Pool" + signature
    page = ""
    values = str(read["limit"]) + parameter
    if pagination:
        page = f"""limit, err := pageValue(queryValue(rawQuery, "limit", {canonical(pagination["limit"])}), 4, 1, 1000)
    if err != nil {{ return nil, err }}
    offset, err := pageValue(queryValue(rawQuery, "offset", {canonical(pagination["offset"])}), 10, 0, 2147483647)
    if err != nil {{ return nil, err }}"""
        values = "limit, offset" + parameter
    values += scoped_values
    return f"""func readRows{index}({arguments}) ([]byte, error) {{
    {page}
    {guard}
    rows, err := pool.Query(ctx, {canonical(query)}, {values})
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


def _detail_read_registration(
    index: int,
    route: dict[str, Any],
    model: dict[str, Any],
    target: str,
    error_key: str,
) -> str:
    field = next(item for item in model["fields"] if item["name"] == route["read"]["lookup"])
    bits = "32" if field["go_type"] == "int32" else "64"
    name = canonical(field["name"])
    reader = "readRows" if route["read"].get("many") else "readRow"
    path = route["path"]
    if target == "fiber":
        return f"""app.Get({canonical(path)}, func(c fiber.Ctx) error {{
        rawID, parseErr := strconv.ParseInt(c.Params({name}), 10, {bits})
        if parseErr != nil {{ return c.Status(400).JSON(fiber.Map{{{canonical(error_key)}: "invalid lookup"}}) }}
        body, err := {reader}{index}(c.Context(), pool, {field["go_type"]}(rawID))
        c.Set("Content-Type", "application/json")
        if errors.Is(err, pgx.ErrNoRows) {{ return c.Status(404).JSON(fiber.Map{{{canonical(error_key)}: "not found"}}) }}
        if err != nil {{ return c.Status(500).Send([]byte(`{{"error":"database read failed"}}`)) }}
        return c.Send(body)
    }})"""
    if target == "gin":
        return f"""app.GET({canonical(path)}, func(c *gin.Context) {{
        rawID, parseErr := strconv.ParseInt(c.Param({name}), 10, {bits})
        if parseErr != nil {{ c.JSON(400, gin.H{{{canonical(error_key)}: "invalid lookup"}}); return }}
        body, err := {reader}{index}(c.Request.Context(), pool, {field["go_type"]}(rawID))
        if errors.Is(err, pgx.ErrNoRows) {{ c.JSON(404, gin.H{{{canonical(error_key)}: "not found"}}); return }}
        if err != nil {{ c.Data(500, "application/json", []byte(`{{"error":"database read failed"}}`)); return }}
        c.Data(200, "application/json", body)
    }})"""
    registered_path = path.replace(f":{field['name']}", f"{{{field['name']}}}")
    parameter = f"chi.URLParam(r, {name})" if target == "chi" else f"mux.Vars(r)[{name}]"
    registration = (
        f'app.MethodFunc("GET", {canonical(registered_path)},'
        if target == "chi"
        else f"app.HandleFunc({canonical(registered_path)},"
    )
    suffix = ")" if target == "chi" else ').Methods("GET")'
    return f"""{registration} func(w http.ResponseWriter, r *http.Request) {{
        rawID, parseErr := strconv.ParseInt({parameter}, 10, {bits})
        w.Header().Set("Content-Type", "application/json")
        if parseErr != nil {{ w.WriteHeader(400); _ = json.NewEncoder(w).Encode(map[string]string{{{canonical(error_key)}: "invalid lookup"}}); return }}
        body, err := {reader}{index}(r.Context(), pool, {field["go_type"]}(rawID))
        if errors.Is(err, pgx.ErrNoRows) {{ w.WriteHeader(404); _ = json.NewEncoder(w).Encode(map[string]string{{{canonical(error_key)}: "not found"}}); return }}
        if err != nil {{ w.WriteHeader(500); _, _ = w.Write([]byte(`{{"error":"database read failed"}}`)); return }}
        _, _ = w.Write(body)
    }}{suffix}"""


def _detail_read_helper(index: int, read: dict[str, Any], model: dict[str, Any]) -> str:
    fields = model["fields"]
    lookup = next(field for field in fields if field["name"] == read["lookup"])
    columns = ", ".join('"' + field["name"] + '"' for field in fields)
    destinations = ", ".join("&item." + go_name(field["name"]) for field in fields)
    query = f'SELECT {columns} FROM "{model["table"]}" WHERE "{lookup["name"]}" = $1'
    guard, predicate, values = _scope_parts(read, 2, "return nil, scopeErr")
    if predicate:
        query += " AND " + predicate
    return f"""func readRow{index}(ctx context.Context, pool *pgxpool.Pool, lookup {lookup["go_type"]}) ([]byte, error) {{
    {guard}
    var item {model["name"]}
    if err := pool.QueryRow(ctx, {canonical(query)}, lookup{values}).Scan({destinations}); err != nil {{ return nil, err }}
    return json.Marshal(item)
}}
"""


def _delete_registration(
    index: int,
    route: dict[str, Any],
    model: dict[str, Any],
    target: str,
    error_key: str,
    source: str,
) -> str:
    content_type = {"flask": "text/html; charset=utf-8", "fastapi": "application/json", "drf": ""}[
        source
    ]
    field = next(item for item in model["fields"] if item["name"] == route["write"]["lookup"])
    bits = "32" if field["go_type"] == "int32" else "64"
    name = canonical(field["name"])
    status = route["status"]
    path = route["path"]
    if target == "fiber":
        header = (
            f'c.Set("Content-Type", {canonical(content_type)})'
            if content_type
            else 'c.Response().Header.Del("Content-Type"); c.Response().Header.SetNoDefaultContentType(true)'
        )
        return f"""app.Delete({canonical(path)}, func(c fiber.Ctx) error {{
        rawID, parseErr := strconv.ParseInt(c.Params({name}), 10, {bits})
        if parseErr != nil {{ return c.Status(400).JSON(fiber.Map{{{canonical(error_key)}: "invalid lookup"}}) }}
        err := deleteRow{index}(c.Context(), pool, {field["go_type"]}(rawID))
        if errors.Is(err, pgx.ErrNoRows) {{ return c.Status(404).JSON(fiber.Map{{{canonical(error_key)}: "not found"}}) }}
        if err != nil {{ return c.Status(500).JSON(fiber.Map{{"error": "database write failed"}}) }}
        {header}
        return c.Status({status}).Send(nil)
    }})"""
    if target == "gin":
        return f"""app.DELETE({canonical(path)}, func(c *gin.Context) {{
        rawID, parseErr := strconv.ParseInt(c.Param({name}), 10, {bits})
        if parseErr != nil {{ c.JSON(400, gin.H{{{canonical(error_key)}: "invalid lookup"}}); return }}
        err := deleteRow{index}(c.Request.Context(), pool, {field["go_type"]}(rawID))
        if errors.Is(err, pgx.ErrNoRows) {{ c.JSON(404, gin.H{{{canonical(error_key)}: "not found"}}); return }}
        if err != nil {{ c.JSON(500, gin.H{{"error": "database write failed"}}); return }}
        c.Header("Content-Type", {canonical(content_type)})
        c.Status({status})
    }})"""
    registered_path = path.replace(f":{field['name']}", f"{{{field['name']}}}")
    parameter = f"chi.URLParam(r, {name})" if target == "chi" else f"mux.Vars(r)[{name}]"
    registration = (
        f'app.MethodFunc("DELETE", {canonical(registered_path)},'
        if target == "chi"
        else f"app.HandleFunc({canonical(registered_path)},"
    )
    suffix = ")" if target == "chi" else ').Methods("DELETE")'
    return f"""{registration} func(w http.ResponseWriter, r *http.Request) {{
        rawID, parseErr := strconv.ParseInt({parameter}, 10, {bits})
        if parseErr != nil {{ writeResponse(w, 400, map[string]string{{{canonical(error_key)}: "invalid lookup"}}); return }}
        err := deleteRow{index}(r.Context(), pool, {field["go_type"]}(rawID))
        if errors.Is(err, pgx.ErrNoRows) {{ writeResponse(w, 404, map[string]string{{{canonical(error_key)}: "not found"}}); return }}
        if err != nil {{ writeResponse(w, 500, map[string]string{{"error": "database write failed"}}); return }}
        {f'w.Header().Set("Content-Type", {canonical(content_type)})' if content_type else ""}
        w.WriteHeader({status})
    }}{suffix}"""


def _delete_helper(index: int, write: dict[str, Any], model: dict[str, Any]) -> str:
    lookup = next(field for field in model["fields"] if field["name"] == write["lookup"])
    query = f'DELETE FROM "{model["table"]}" WHERE "{lookup["name"]}" = $1'
    guard, predicate, values = _scope_parts(write, 2, "return scopeErr")
    if predicate:
        query += " AND " + predicate
    return f"""func deleteRow{index}(ctx context.Context, pool *pgxpool.Pool, lookup {lookup["go_type"]}) error {{
    {guard}
    return pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {{
        result, err := tx.Exec(ctx, {canonical(query)}, lookup{values})
        if err != nil {{ return err }}
        if result.RowsAffected() == 0 {{ return pgx.ErrNoRows }}
        return nil
    }})
}}
"""


def _transaction_helper(index: int, write: dict[str, Any], models: list[dict[str, Any]]) -> str:
    by_name = {model["name"]: model for model in models}
    steps = write["transaction"]
    result_type = "json.RawMessage" if "literal_response" in write else write["model"]
    failure = f"return {result_type}{{}}, errInvalidWrite"
    decoders = []
    decoding = []
    statements = []
    for number, step in enumerate(steps):
        model = by_name[step["model"]]
        fields = model["fields"]
        operation = step.get("operation", "create")
        primary = next(field for field in fields if field["primary_key"])
        input_fields = [
            dict(field, auto=False) if field["primary_key"] and operation != "create" else field
            for field in fields
            if field["name"] not in step["references"]
            and not (field["primary_key"] and step.get("lookup_reference"))
            and (operation not in {"lookup", "delete"} or field["primary_key"])
        ]
        input_model = dict(model, fields=input_fields)
        decoder_name = f"decodeTransaction{index}Step{number}"
        decoders.append(_write_helper(index, {}, input_model, True, decoder_only=decoder_name))
        seen = f"seen{number}" if operation == "patch" else "_"
        decoding.append(
            f"item{number}, {seen}, err := {decoder_name}(values[{canonical(step['input'])}], {str(operation == 'patch').lower()})\n    if err != nil {{ {failure} }}"
        )
        if operation == "patch" and not step.get("lookup_reference"):
            decoding.append(f"if !{seen}[{canonical(primary['name'])}] {{ {failure} }}")
        for field in fields:
            reference = step["references"].get(field["name"])
            if field["primary_key"] and step.get("lookup_reference"):
                reference = step["lookup_reference"]
            if reference:
                source = f"saved{reference['step']}.{go_name(reference['field'])}"
                statements.append(
                    f"item{number}.{go_name(field['name'])} = {'&' if field['nullable'] else ''}{source}"
                )
        writable = [field for field in fields if not field["auto"]]
        columns = ", ".join('"' + field["name"] + '"' for field in writable)
        returning = ", ".join('"' + field["name"] + '"' for field in fields)
        destinations = ", ".join(f"&saved{number}." + go_name(field["name"]) for field in fields)
        key = f"item{number}.{go_name(primary['name'])}"
        if operation == "create":
            placeholders = ", ".join(f"${i}" for i in range(1, len(writable) + 1))
            arguments = ", ".join(f"item{number}." + go_name(field["name"]) for field in writable)
            query = f'INSERT INTO "{model["table"]}" ({columns}) VALUES ({placeholders}) RETURNING {returning}'
            statements.append(
                f"if err := tx.QueryRow(ctx, {canonical(query)}, {arguments}).Scan({destinations}); err != nil {{ return err }}"
            )
            continue
        select = f'SELECT {returning} FROM "{model["table"]}" WHERE "{primary["name"]}" = $1'
        statements.append(
            f"if err := tx.QueryRow(ctx, {canonical(select)}, {key}).Scan({destinations}); err != nil {{ return err }}"
        )
        if operation == "lookup":
            continue
        if operation == "delete":
            query = f'DELETE FROM "{model["table"]}" WHERE "{primary["name"]}" = $1'
            statements.append(
                f"if _, err := tx.Exec(ctx, {canonical(query)}, {key}); err != nil {{ return err }}"
            )
            continue
        if operation == "replace":
            sets = ", ".join(f'"{field["name"]}" = ${i}' for i, field in enumerate(writable, 1))
            arguments = ", ".join(f"item{number}." + go_name(field["name"]) for field in writable)
            query = f'UPDATE "{model["table"]}" SET {sets} WHERE "{primary["name"]}" = ${len(writable) + 1} RETURNING {returning}'
            statements.append(
                f"if err := tx.QueryRow(ctx, {canonical(query)}, {arguments}, {key}).Scan({destinations}); err != nil {{ return transactionUpdateError(err) }}"
            )
        else:
            statements.append(f"sets{number} := []string{{}}; args{number} := []any{{}}")
            for field in writable:
                column = canonical('"' + field["name"] + '" = $%d')
                statements.append(
                    f"if {seen}[{canonical(field['name'])}] {{ args{number} = append(args{number}, item{number}.{go_name(field['name'])}); sets{number} = append(sets{number}, fmt.Sprintf({column}, len(args{number}))) }}"
                )
            query = f'UPDATE "{model["table"]}" SET %s WHERE "{primary["name"]}" = $%d RETURNING {returning}'
            statements.append(f"""if len(sets{number}) > 0 {{
            args{number} = append(args{number}, {key})
            query := fmt.Sprintf({canonical(query)}, strings.Join(sets{number}, ", "), len(args{number}))
            if err := tx.QueryRow(ctx, query, args{number}...).Scan({destinations}); err != nil {{ return transactionUpdateError(err) }}
        }}""")
    declarations = "\n    ".join(f"var saved{i} {step['model']}" for i, step in enumerate(steps))
    allowed = ", ".join(canonical(step["input"]) + ": {}" for step in steps)
    response = (
        f"json.RawMessage({canonical(canonical(write['literal_response']))})"
        if "literal_response" in write
        else f"saved{len(steps) - 1}"
    )
    helper = f"""func writeRow{index}(ctx context.Context, pool *pgxpool.Pool, body []byte) ({result_type}, error) {{
    var values map[string]json.RawMessage
    decoder := json.NewDecoder(bytes.NewReader(body))
    if err := decoder.Decode(&values); err != nil || values == nil {{ {failure} }}
    if err := decoder.Decode(&struct{{}}{{}}); err != io.EOF {{ {failure} }}
    allowed := map[string]struct{{}}{{{allowed}}}
    if len(values) != len(allowed) {{ {failure} }}
    for key := range values {{
        if _, ok := allowed[key]; !ok {{ {failure} }}
    }}
    {chr(10).join(decoding)}
    {declarations}
    err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {{
        {chr(10).join(statements)}
        return nil
    }})
    return {response}, err
}}
"""
    return "\n".join(decoders) + helper


def _write_helper(
    index: int,
    write: dict[str, Any],
    model: dict[str, Any],
    include_decoder: bool,
    *,
    decoder_only: str = "",
) -> str:
    fields = model["fields"]
    writable = [field for field in fields if not field["auto"]]
    columns = ", ".join('"' + field["name"] + '"' for field in writable)
    returning = ", ".join('"' + field["name"] + '"' for field in fields)
    destinations = ", ".join("&saved." + go_name(field["name"]) for field in fields)
    accepted = ", ".join(canonical(field["name"]) + ": {}" for field in writable)
    validation_kind = write.get("validation", {}).get("kind", "strict")
    decoding = []
    required = []
    for field in writable:
        name = canonical(field["name"])
        target = "item." + go_name(field["name"])
        null_guard = ""
        if not field["nullable"]:
            null_guard = ' || bytes.Equal(bytes.TrimSpace(raw), []byte("null"))'
            required.append(f"if !partial && !seen[{name}] {{ return item, nil, errInvalidWrite }}")
        if validation_kind in {"pydantic", "drf"}:
            value_type = {
                "string": "string",
                "int32": "int32",
                "int64": "int64",
                "bool": "bool",
            }[field["go_type"]]
            if validation_kind == "pydantic":
                conversion = {
                    "string": "var value string\n            err := json.Unmarshal(raw, &value)",
                    "int32": "number, err := pydanticInt(raw, 32)\n            value := int32(number)",
                    "int64": "value, err := pydanticInt(raw, 64)",
                    "bool": "value, err := pydanticBool(raw)",
                }[field["go_type"]]
            else:
                conversion = {
                    "string": "value, err := drfString(raw)",
                    "int32": "number, err := drfInt(raw, 32)\n            value := int32(number)",
                    "int64": "value, err := drfInt(raw, 64)",
                    "bool": "value, err := drfBool(raw)",
                }[field["go_type"]]
            if field["nullable"]:
                null_value = 'bytes.Equal(bytes.TrimSpace(raw), []byte("null"))'
                if validation_kind == "drf" and field["go_type"] == "bool":
                    null_value += ' || bytes.Equal(bytes.TrimSpace(raw), []byte(`""`))'
                code = f"""if {null_value} {{
            {target} = nil
        }} else {{
            {conversion}
            if err != nil {{ return item, nil, errInvalidWrite }}
            converted := {value_type}(value)
            {target} = &converted
        }}"""
            else:
                code = f"""if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {{ return item, nil, errInvalidWrite }}
        {conversion}
        if err != nil {{ return item, nil, errInvalidWrite }}
        {target} = {value_type}(value)"""
                if field["go_type"] == "int32":
                    code = code.replace(f"{target} = int32(value)", f"{target} = value")
            decoding.append(
                f"""if raw, ok := values[{name}]; ok {{
        {code}
        seen[{name}] = true
    }}"""
            )
        else:
            decoding.append(
                f"""if raw, ok := values[{name}]; ok {{
        if err := json.Unmarshal(raw, &{target}); err != nil{null_guard} {{
            return item, nil, errInvalidWrite
        }}
        seen[{name}] = true
    }}"""
            )
    custom_constraints = write.get("constraints", {})
    constraints = dict(custom_constraints)
    if validation_kind == "drf":
        for field in writable:
            match = re.fullmatch(r"varchar\((\d+)\)", field["sql_type"])
            if match:
                constraints[field["name"]] = {"max_length": int(match.group(1))}
    decoder_name = f"decode{model['name']}"
    if validation_kind in {"pydantic", "drf"}:
        decoder_name += f"_{validation_kind}"
    if custom_constraints:
        decoder_name += f"_write{index}"
    if decoder_only:
        decoder_name = decoder_only
    for field in writable:
        target = "item." + go_name(field["name"])
        bounds = constraints.get(field["name"], {})
        tests = []
        for key, bound in sorted(bounds.items()):
            value = ("*" if field["nullable"] else "") + target
            if key in {"min_length", "max_length"}:
                value = f"len([]rune({value}))"
            tests.append(f"{value} {'<' if key in {'ge', 'min_length'} else '>'} {bound}")
        if tests:
            guard = f"seen[{canonical(field['name'])}]"
            if field["nullable"]:
                guard += f" && {target} != nil"
            decoding.append(
                f"if {guard} && ({' || '.join(tests)}) {{ return item, nil, errInvalidWrite }}"
            )
    decoder = f"""func {decoder_name}(body []byte, partial bool) ({model["name"]}, map[string]bool, error) {{
    var item {model["name"]}
    var values map[string]json.RawMessage
    decoder := json.NewDecoder(bytes.NewReader(body))
    if err := decoder.Decode(&values); err != nil || values == nil {{
        return item, nil, errInvalidWrite
    }}
    if err := decoder.Decode(&struct{{}}{{}}); err != io.EOF {{
        return item, nil, errInvalidWrite
    }}
    allowed := map[string]struct{{}}{{{accepted}}}
    for name := range values {{
        if _, ok := allowed[name]; !ok {{ {"delete(values, name)" if validation_kind in {"pydantic", "drf"} else "return item, nil, errInvalidWrite"} }}
    }}
    seen := map[string]bool{{}}
    {chr(10).join(decoding)}
    {chr(10).join(required)}
    return item, seen, nil
}}
"""
    if decoder_only:
        return decoder
    operation = write["operation"]
    scope = write.get("scope", {})
    guard, _, _ = _scope_parts(write, 1, f"return {model['name']}{{}}, scopeErr")
    body_guard = "\n    ".join(
        f"if seen[{canonical(field)}] && item.{go_name(field)} != principal[{canonical(claim)}] {{ return {model['name']}{{}}, errScopeDenied }}"
        for field, claim in scope.items()
    )
    seen_name = "seen" if scope else "_"
    if operation == "create":
        placeholders = ", ".join(f"${number}" for number in range(1, len(writable) + 1))
        arguments = ", ".join("item." + go_name(field["name"]) for field in writable)
        query = f'INSERT INTO "{model["table"]}" ({columns}) VALUES ({placeholders}) RETURNING {returning}'
        helper = f"""func writeRow{index}(ctx context.Context, pool *pgxpool.Pool, body []byte) ({model["name"]}, error) {{
    {guard}
    item, {seen_name}, err := {decoder_name}(body, false)
    if err != nil {{ return {model["name"]}{{}}, err }}
    {body_guard}
    var saved {model["name"]}
    err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {{
        return tx.QueryRow(ctx, {canonical(query)}, {arguments}).Scan({destinations})
    }})
    return saved, err
}}
"""
    elif operation == "replace":
        lookup = next(field for field in fields if field["name"] == write["lookup"])
        sets = ", ".join(
            f'"{field["name"]}" = ${number}' for number, field in enumerate(writable, 1)
        )
        arguments = ", ".join("item." + go_name(field["name"]) for field in writable)
        query = (
            f'UPDATE "{model["table"]}" SET {sets} WHERE "{lookup["name"]}" = ${len(writable) + 1} '
            f"RETURNING {returning}"
        )
        _, predicate, scope_values = _scope_parts(write, len(writable) + 2, "")
        if predicate:
            query = query.replace(" RETURNING", " AND " + predicate + " RETURNING")
        helper = f"""func writeRow{index}(ctx context.Context, pool *pgxpool.Pool, body []byte, lookup {lookup["go_type"]}) ({model["name"]}, error) {{
    {guard}
    item, {seen_name}, err := {decoder_name}(body, false)
    if err != nil {{ return {model["name"]}{{}}, err }}
    {body_guard}
    var saved {model["name"]}
    err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {{
        return tx.QueryRow(ctx, {canonical(query)}, {arguments}, lookup{scope_values}).Scan({destinations})
    }})
    return saved, err
}}
"""
    else:
        lookup = next(field for field in fields if field["name"] == write["lookup"])
        cases = []
        for field in writable:
            name = canonical(field["name"])
            cases.append(
                f"""if seen[{name}] {{
            values = append(values, item.{go_name(field["name"])})
            sets = append(sets, fmt.Sprintf({canonical('"' + field["name"] + '" = $%d')}, len(values)))
        }}"""
            )
        select = f'SELECT {returning} FROM "{model["table"]}" WHERE "{lookup["name"]}" = $1'
        update = (
            f'UPDATE "{model["table"]}" SET %s WHERE "{lookup["name"]}" = $%d RETURNING {returning}'
        )
        _, predicate, scope_values = _scope_parts(write, 2, "")
        if predicate:
            select += " AND " + predicate
        _, predicate, initial_values = _scope_parts(write, 1, "")
        if predicate:
            update = update.replace(" RETURNING", " AND " + predicate + " RETURNING")
        helper = f"""func writeRow{index}(ctx context.Context, pool *pgxpool.Pool, body []byte, lookup {lookup["go_type"]}) ({model["name"]}, error) {{
    {guard}
    item, seen, err := {decoder_name}(body, true)
    if err != nil {{ return {model["name"]}{{}}, err }}
    {body_guard}
    var saved {model["name"]}
    err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {{
        sets := []string{{}}
        values := []any{{{initial_values.removeprefix(", ")}}}
        {chr(10).join(cases)}
        if len(sets) == 0 {{
            return tx.QueryRow(ctx, {canonical(select)}, lookup{scope_values}).Scan({destinations})
        }}
        values = append(values, lookup)
        query := fmt.Sprintf({canonical(update)}, strings.Join(sets, ", "), len(values))
        return tx.QueryRow(ctx, query, values...).Scan({destinations})
    }})
    return saved, err
}}
"""
    return (decoder if include_decoder or custom_constraints else "") + helper


PYDANTIC_HELPERS = r"""
func pydanticInt(raw []byte, bits int) (int64, error) {
    var input any
    decoder := json.NewDecoder(bytes.NewReader(raw))
    decoder.UseNumber()
    if err := decoder.Decode(&input); err != nil { return 0, errInvalidWrite }
    var text string
    fromString := false
    switch value := input.(type) {
    case bool:
        if value { return 1, nil }
        return 0, nil
    case json.Number:
        text = string(value)
    case string:
        text = strings.TrimSpace(value)
        fromString = true
    default:
        return 0, errInvalidWrite
    }
    if fromString && !strings.ContainsAny(text, "eE") {
        if dot := strings.IndexByte(text, '.'); dot >= 0 && strings.Trim(text[dot+1:], "0") == "" {
            text = text[:dot]
        }
    }
    if value, err := strconv.ParseInt(text, 10, bits); err == nil { return value, nil }
    value, err := strconv.ParseFloat(text, 64)
    if err != nil || math.IsNaN(value) || math.IsInf(value, 0) || math.Trunc(value) != value || (fromString && strings.ContainsAny(text, "eE")) {
        return 0, errInvalidWrite
    }
    if bits == 64 && (value < -9223372036854775808.0 || value >= 9223372036854775808.0) {
        return 0, errInvalidWrite
    }
    integer := int64(value)
    if bits == 32 && (integer < math.MinInt32 || integer > math.MaxInt32) {
        return 0, errInvalidWrite
    }
    return integer, nil
}

func pydanticBool(raw []byte) (bool, error) {
    var input any
    decoder := json.NewDecoder(bytes.NewReader(raw))
    decoder.UseNumber()
    if err := decoder.Decode(&input); err != nil { return false, errInvalidWrite }
    switch value := input.(type) {
    case bool:
        return value, nil
    case json.Number:
        number, err := strconv.ParseFloat(string(value), 64)
        if err == nil && number == 0 { return false, nil }
        if err == nil && number == 1 { return true, nil }
    case string:
        switch strings.ToLower(value) {
        case "0", "off", "f", "false", "n", "no":
            return false, nil
        case "1", "on", "t", "true", "y", "yes":
            return true, nil
        }
    }
    return false, errInvalidWrite
}
"""


DRF_HELPERS = r"""
func drfString(raw []byte) (string, error) {
    var input any
    decoder := json.NewDecoder(bytes.NewReader(raw))
    decoder.UseNumber()
    if err := decoder.Decode(&input); err != nil { return "", errInvalidWrite }
    switch value := input.(type) {
    case string:
        return value, nil
    case json.Number:
        return string(value), nil
    }
    return "", errInvalidWrite
}

func drfInt(raw []byte, bits int) (int64, error) {
    var input any
    decoder := json.NewDecoder(bytes.NewReader(raw))
    decoder.UseNumber()
    if err := decoder.Decode(&input); err != nil { return 0, errInvalidWrite }
    var text string
    fromString := false
    switch value := input.(type) {
    case json.Number:
        text = string(value)
    case string:
        text = strings.TrimSpace(value)
        fromString = true
    default:
        return 0, errInvalidWrite
    }
    if fromString && !strings.ContainsAny(text, "eE") {
        if dot := strings.IndexByte(text, '.'); dot >= 0 && strings.Trim(text[dot+1:], "0") == "" {
            text = text[:dot]
        }
    }
    if value, err := strconv.ParseInt(text, 10, bits); err == nil { return value, nil }
    value, err := strconv.ParseFloat(text, 64)
    if err != nil || math.IsNaN(value) || math.IsInf(value, 0) || math.Trunc(value) != value || (fromString && strings.ContainsAny(text, "eE")) {
        return 0, errInvalidWrite
    }
    if bits == 64 && (value < -9223372036854775808.0 || value >= 9223372036854775808.0) {
        return 0, errInvalidWrite
    }
    integer := int64(value)
    if bits == 32 && (integer < math.MinInt32 || integer > math.MaxInt32) {
        return 0, errInvalidWrite
    }
    return integer, nil
}

func drfBool(raw []byte) (bool, error) {
    var input any
    decoder := json.NewDecoder(bytes.NewReader(raw))
    decoder.UseNumber()
    if err := decoder.Decode(&input); err != nil { return false, errInvalidWrite }
    switch value := input.(type) {
    case bool:
        return value, nil
    case json.Number:
        number, err := strconv.ParseFloat(string(value), 64)
        if err == nil && number == 0 { return false, nil }
        if err == nil && number == 1 { return true, nil }
    case string:
        switch strings.ToLower(value) {
        case "0", "off", "f", "false", "n", "no":
            return false, nil
        case "1", "on", "t", "true", "y", "yes":
            return true, nil
        }
    }
    return false, errInvalidWrite
}
"""


PAGINATION_SOURCE = """// SPDX-License-Identifier: Apache-2.0
package backend
import "errors"
var errInvalidPage = errors.New("invalid pagination")
func pageValue(raw string, digits int, low, high int64) (int64, error) {
    if len(raw) == 0 || len(raw) > digits { return 0, errInvalidPage }
    var value int64
    for i := 0; i < len(raw); i++ {
        if raw[i] < '0' || raw[i] > '9' { return 0, errInvalidPage }
        value = value*10 + int64(raw[i]-'0')
    }
    if value < low || value > high { return 0, errInvalidPage }
    return value, nil
}
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
