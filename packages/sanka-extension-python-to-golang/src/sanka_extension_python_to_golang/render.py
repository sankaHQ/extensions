# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Pinned framework adapters for captured JSON contracts."""

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


def _fiber_runtime(database: bool, database_configured: bool) -> dict[str, str]:
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

    "github.com/gofiber/fiber/v3"
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
{database_setup}    return app.Listen(cfg.address, fiber.ListenConfig{{
        DisableStartupMessage: true,
        GracefulContext: runCtx,
        ShutdownTimeout: 10*time.Second,
    }})
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
    if has_writes and target != "fiber":
        raise ValueError("writes are qualified only for Fiber")
    module = MODULES[target]
    error_key = "detail" if captured["configuration"]["source_framework"] == "fastapi" else "error"
    registrations = []
    helpers = []
    write_models: set[str] = set()
    has_reads = any("read" in route for route in captured["routes"])
    for index, route in enumerate(captured["routes"]):
        path = canonical(route["path"])
        if "write" in route:
            model = next(
                item for item in captured["models"] if item["name"] == route["write"]["model"]
            )
            helpers.append(
                _write_helper(index, route["write"], model, model["name"] not in write_models)
            )
            write_models.add(model["name"])
            method = route["method"].title()
            status = route["status"]
            lookup = ""
            if route["write"]["operation"] == "patch":
                field = next(
                    item for item in model["fields"] if item["name"] == route["write"]["lookup"]
                )
                bits = "32" if field["go_type"] == "int32" else "64"
                lookup = f"""rawID, parseErr := strconv.ParseInt(c.Params({canonical(field["name"])}), 10, {bits})
        if parseErr != nil {{ return c.Status(400).JSON(fiber.Map{{{canonical(error_key)}: "invalid lookup"}}) }}
        lookup := {field["go_type"]}(rawID)"""
            argument = ", lookup" if lookup else ""
            registrations.append(f"""app.{method}({path}, func(c fiber.Ctx) error {{
        {lookup}
        item, err := writeRow{index}(c.Context(), pool, c.Body(){argument})
        if err != nil {{
            if errors.Is(err, errInvalidWrite) {{
                return c.Status(400).JSON(fiber.Map{{{canonical(error_key)}: "invalid request body"}})
            }}
            if errors.Is(err, pgx.ErrNoRows) {{
                return c.Status(404).JSON(fiber.Map{{{canonical(error_key)}: "not found"}})
            }}
            return c.Status(500).JSON(fiber.Map{{"error": "database write failed"}})
        }}
        return c.Status({status}).JSON(item)
    }})""")
            continue
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
    return_type = "*fiber.App" if target == "fiber" else "http.Handler"
    http_import = "" if target == "fiber" else '"net/http"\n'
    database = has_reads or has_writes
    imports = []
    if has_reads:
        imports.extend(['"context"', '"encoding/json"'])
    if target == "fiber":
        imports.append('"time"')
    if has_writes:
        imports.extend(
            [
                '"bytes"',
                '"context"',
                '"encoding/json"',
                '"errors"',
                '"io"',
                '"github.com/jackc/pgx/v5"',
            ]
        )
    if has_patch:
        imports.extend(['"fmt"', '"strconv"', '"strings"'])
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
    return app
}}
{chr(10).join(helpers)}
'''
    if has_writes:
        source += '\nvar errInvalidWrite = errors.New("invalid write")\n'
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
    if target == "fiber":
        result.update(
            _fiber_runtime(database, captured["configuration"]["database_layer"] == "pgx")
        )
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


def _write_helper(
    index: int, write: dict[str, Any], model: dict[str, Any], include_decoder: bool
) -> str:
    fields = model["fields"]
    writable = [field for field in fields if not field["auto"]]
    columns = ", ".join('"' + field["name"] + '"' for field in writable)
    returning = ", ".join('"' + field["name"] + '"' for field in fields)
    destinations = ", ".join("&saved." + go_name(field["name"]) for field in fields)
    accepted = ", ".join(canonical(field["name"]) + ": {}" for field in writable)
    decoding = []
    required = []
    for field in writable:
        name = canonical(field["name"])
        target = "item." + go_name(field["name"])
        null_guard = ""
        if not field["nullable"]:
            null_guard = ' || bytes.Equal(bytes.TrimSpace(raw), []byte("null"))'
            required.append(f"if !partial && !seen[{name}] {{ return item, nil, errInvalidWrite }}")
        decoding.append(
            f"""if raw, ok := values[{name}]; ok {{
        if err := json.Unmarshal(raw, &{target}); err != nil{null_guard} {{
            return item, nil, errInvalidWrite
        }}
        seen[{name}] = true
    }}"""
        )
    decoder = f"""func decode{model["name"]}(body []byte, partial bool) ({model["name"]}, map[string]bool, error) {{
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
        if _, ok := allowed[name]; !ok {{ return item, nil, errInvalidWrite }}
    }}
    seen := map[string]bool{{}}
    {chr(10).join(decoding)}
    {chr(10).join(required)}
    return item, seen, nil
}}
"""
    operation = write["operation"]
    if operation == "create":
        placeholders = ", ".join(f"${number}" for number in range(1, len(writable) + 1))
        arguments = ", ".join("item." + go_name(field["name"]) for field in writable)
        query = f'INSERT INTO "{model["table"]}" ({columns}) VALUES ({placeholders}) RETURNING {returning}'
        helper = f"""func writeRow{index}(ctx context.Context, pool *pgxpool.Pool, body []byte) ({model["name"]}, error) {{
    item, _, err := decode{model["name"]}(body, false)
    if err != nil {{ return {model["name"]}{{}}, err }}
    var saved {model["name"]}
    err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {{
        return tx.QueryRow(ctx, {canonical(query)}, {arguments}).Scan({destinations})
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
        helper = f"""func writeRow{index}(ctx context.Context, pool *pgxpool.Pool, body []byte, lookup {lookup["go_type"]}) ({model["name"]}, error) {{
    item, seen, err := decode{model["name"]}(body, true)
    if err != nil {{ return {model["name"]}{{}}, err }}
    var saved {model["name"]}
    err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {{
        sets := []string{{}}
        values := []any{{}}
        {chr(10).join(cases)}
        if len(sets) == 0 {{
            return tx.QueryRow(ctx, {canonical(select)}, lookup).Scan({destinations})
        }}
        values = append(values, lookup)
        query := fmt.Sprintf({canonical(update)}, strings.Join(sets, ", "), len(values))
        return tx.QueryRow(ctx, query, values...).Scan({destinations})
    }})
    return saved, err
}}
"""
    return (decoder if include_decoder else "") + helper


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
