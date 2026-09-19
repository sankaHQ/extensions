# TypeScript to Rust (experimental)

Extension ID: `sanka/typescript-to-rust`. Sources: Express (TypeScript).
Targets: axum.

The current implementation produces a Rust crate exposing `migrated_backend::app()`
for literal public JSON GET endpoints and, with the optional `sqlx` database layer,
a flat PostgreSQL schema baseline with bounded table reads. It is **not a complete
backend migration**, not published in the extension catalog, and not qualified for
production cutover. It copies the shape of `sanka/python-to-golang`; the remaining
backend slices (typed parameters and writes, Fastify and Hono sources) follow the
workspace plan.

Use the existing extension JSON subprocess protocol via
`sanka-extension-typescript-to-rust`, with configuration:

```json
{"source_framework":"express","target_framework":"axum","source_file":"src/app.ts"}
```

`target` is accepted as an alias of `target_framework` so the CLI can forward
its `--to` selection; both must agree when present.

`scan` reports unsupported constructs. `plan` contains deterministic generated
files and a `plan_hash`. `apply` requires the runtime review attestation
(`reviewed_plan_hash`) and that hash as `extension_plan_hash`, recomputes the plan
from the current source and configuration, and refuses changed plans or existing
output. Artifacts must be inside the project's `.sanka` directory; output is
`rust/` within that artifact directory.

## What scan captures

Source files are parsed with the vendored TypeScript compiler through
`sanka-ts-capture`; they are never imported or executed. The capture accepts:

- `package.json` declaring `express` 4 or 5 in `dependencies`;
- one application module (`src/app.ts` by default) containing, in order, a default
  import of `express` (type-only named imports are ignored), `const app = express()`,
  optional `app.use(express.json())`, `app.get("/literal/path", handler)`
  registrations, and `export default app` (or `export { app }` / `export const app`);
- inline handlers `(req, res) => res.json({...})`, `res.status(201).json({...})`,
  with block or expression bodies and an optional `async` modifier, whose payload is
  a pure object literal;
- optionally one launcher module (`server.ts` or `index.ts` beside the application
  module) that imports the application and calls `app.listen(...)`.

Every other module, import, middleware, router, non-GET route, dynamic path,
parameter, request access, non-literal payload, `interface`-free type usage aside,
or syntax error is a gap. Gaps block `apply`.

Payload serialization follows JavaScript: numbers are IEEE doubles (an integer
above 2^53 is rounded exactly as Express would round it), keys keep JavaScript's
property order, and the generated handler returns the exact `JSON.stringify`
bytes that `res.json` would send.

## Database layer

`"database_layer": "sqlx"` adds the flat-schema slice. The optional keys
`database_dialect` (`postgresql`), `migration_tool` (`sqlx`), `schema_mode`
(`empty`), `schema_file` (`schema.sql`) and `models_file` (`src/models.ts`) accept
only those values; other dialects, migration tools and existing-schema adoption
are gaps. The capture then accepts, in addition to the literal endpoints:

- `schema.sql` containing only `CREATE TABLE` statements with column definitions:
  `integer`, `bigint`, `serial`, `bigserial`, `boolean`, `text` and `varchar(n)`
  columns, `NOT NULL`, `UNIQUE`, `PRIMARY KEY` (exactly one integer key per table)
  and `GENERATED { BY DEFAULT | ALWAYS } AS IDENTITY` on that key. Defaults,
  foreign keys, checks, table-level constraints, indexes and other statements are
  gaps. Identifiers must already be lowercase.
- `src/models.ts` containing only exported interfaces or object type aliases whose
  members are `number`, `string`, `boolean` or one of them `| null`. Every table
  must match exactly one row type by column names, types and nullability, and
  every row type must match a table.
- `import { Pool } from "pg"` and
  `const pool = new Pool({ connectionString: process.env.DATABASE_URL })`.
- Read handlers of exactly this shape, where the SQL selects every column of one
  table in declaration order, orders by its primary key and binds one literal
  limit between 1 and 1000:

  ```ts
  app.get("/widgets", async (_req, res) => {
    const { rows } = await pool.query("SELECT id, name FROM widgets ORDER BY id LIMIT $1", [100]);
    res.json(rows);
  });
  ```

The generated crate gains `src/models.rs` (`serde::Serialize` + `sqlx::FromRow`
structs), a reversible `sqlx` baseline under `migrations/` that refuses to run
where a captured table already exists, `src/bin/migrate.rs` (`migrate up` /
`migrate down`), `.env.example`, `pub fn app(pool: PgPool) -> Router` and a
`Cargo.lock` pinned under `locks/axum-postgresql` (sqlx 0.8 without TLS). Read
handlers answer `500 {"error":"database read failed"}` when the query fails.
`bigint` columns are serialized as decimal strings because node-postgres returns
them that way; the captured contract is the source's observable output.

## Generated crate

`Cargo.toml`, `Cargo.lock` (pinned in this package under `locks/axum`),
`rust-toolchain.toml` (Rust 1.93.1), `src/lib.rs` with `pub fn app() -> Router`,
`src/main.rs` binding `HOST`/`PORT` (defaults `127.0.0.1:3000`), `contract.json`
and a README. No Rust dependencies are installed into Sanka's Python environment.

Express defaults that are **not** reproduced by the generated crate: case-insensitive
and non-strict (trailing slash) route matching, the `X-Powered-By` and `ETag`
headers, and the `charset=utf-8` suffix on `Content-Type`. Replay compares status,
media type and parsed JSON body for the captured paths only.

## test and verify

`test` copies the generated crate to a temporary directory, adds a
`tower::ServiceExt::oneshot` probe test and runs `cargo test --locked`. It requires
the Rust 1.93.1 toolchain (rustup installs it from `rust-toolchain.toml`; set
`SANKA_RUST_OFFLINE=1` to forbid crate downloads). Build artifacts go to
`CARGO_TARGET_DIR` when set, otherwise to `rust-target/` beside the output. On a
Mac whose Xcode license is not accepted, set
`DEVELOPER_DIR=/Library/Developer/CommandLineTools` so the linker finds the SDK.

`verify` additionally transpiles the captured application module, loads it in
Node.js 22 with the project's installed `express` (from `node_modules`, or the
directory named by `SANKA_NODE_TOOLS`), serves it on a Unix domain socket inside
the temporary directory and compares status, media type and JSON body for every
captured route. Set `SANKA_NODE` when the default `node` is not 22.x. Both
commands require an unchanged saved plan and generated output; each rerun
invalidates its previous report first.

With the database layer, `test` needs `SANKA_RUST_TARGET_TEST_DATABASE_URL` and
`verify` also needs `SANKA_RUST_SOURCE_TEST_DATABASE_URL`: two distinct dedicated
PostgreSQL databases (or schemas selected through `options=-csearch_path=...`)
that already hold the schema and the fixture rows. An ambient `DATABASE_URL` is
never adopted. The Rust candidate receives the target URL as `DATABASE_URL`; the
transpiled source receives the source URL and needs `pg` next to `express` in
`node_modules` or `SANKA_NODE_TOOLS`.

Replay executes source and candidate code; the temporary directory is not a
security sandbox. It does not start TCP listeners or alter candidate files.
Manual edits to `src/lib.rs` are tested; changes to `Cargo.toml`, `Cargo.lock`,
`rust-toolchain.toml`, `contract.json` or the generated migrations are rejected.
