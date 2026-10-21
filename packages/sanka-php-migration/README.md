# Shared PHP migration library

Apache-2.0 static capture, native generation and independent replay shared by
`sanka/php-to-fastapi` and `sanka/php-to-golang`. This is an unreleased candidate.
Neither package is in the public marketplace until the release is approved.

## Supported selection

- Custom PHP: explicitly mapped functions receiving one array request and returning
  `['status' => 200, 'body' => ...]`. The manifest defines the transport adapter.
- Laravel: explicit route closures with an `Illuminate\Http\Request`, literal
  paths, ordered string path parameters, JSON responses, flat string validation,
  and parameterized single-table `DB::select/insert/update/delete` statements.
- Local assignments, conditionals, strict equality, short-circuit booleans, null
  coalescing, explicit bearer guards, `hash_equals`, arrays and literal values.
- One transaction level, including rollback after an exception. MySQL stays
  MySQL; SQLite stays SQLite. Schema adoption and production cutover are separate.

The closure profile uses Laravel's default request normalization. It has no
application service providers, Eloquent models, custom middleware or autoloaders.
Controllers, Sanctum, sessions, queues, dynamic routing, loose comparisons and
unknown dependencies block conversion. WordPress, Drupal and full application
migrations are outside this candidate. A repository without `sanka-php.json`
receives a static assessment; it cannot be applied until its closure is explicit.
A discovered route count is not a complete inventory when the report says
`route_inventory_complete: false`.

## Project manifest

```json
{
  "schema": "sanka.php-project/v1",
  "kind": "custom",
  "database": {"dialect": "none"},
  "routes": [{"method": "GET", "path": "/health", "file": "api.php", "handler": "health"}]
}
```

For Laravel use `"kind": "laravel"` and `"route_files": ["routes/api.php"]` instead
of `routes`. A database selection additionally declares `schema_file` and `tables`.
The schema profile accepts flat integer, text, varchar and decimal columns; it
rejects triggers, custom SQL expressions and undeclared tables.

## Review and verification

Scan and Plan use tree-sitter 0.25.2 with tree-sitter-php 0.24.1. They never execute
PHP, Composer, Artisan or application autoloaders. This avoids bootstrapping
customer code for assessment; the grammar's supported syntax is explicitly bounded.
The shared portable contract opts into `sanka.backend-ir/v2`; v1 plans and saved
hashes keep their existing semantics and database restrictions.

Apply checks the unchanged source, selected endpoints, extension identity and
reviewed plan hash. It owns only its generated output directory. Generated FastAPI
and chi handlers run native code, with explicit PHP compatibility functions.
The target has no PHP subprocess fallback.

Test and Verify require a prepared disposable Linux environment. Acquisition of
pinned Composer, Python and Go dependencies happens before replay. The checked-in
CI workflow executes all source/target/database lanes and deliberately changes a
candidate response to demonstrate a failed Verify. It uses patched PHP 8.4.27,
Laravel 12.70.1, Python 3.12.13 and Go 1.27.0. Dependency locks ship in the wheel.

`SANKA_PHP_REPLAY=1` is an operator attestation that the Linux process is disposable
and isolated; this library does not create the outer sandbox. Hosted workers own
that boundary. MySQL replay accepts only a disposable `sanka_php_admin` service
on loopback or `mysql`; it creates separate random source/target schemas and users,
then deletes them. The parent observes all declared tables and identity sequences
after each request; candidate code cannot submit its own database evidence.

Provide ordered requests in `sanka-verify.json` using `sanka.http-scenarios/v1`.
Every selected route needs at least one scenario with an independent expected
status. Verify compares actual original PHP against the edited native candidate:
HTTP status, JSON media type and body, declared database contents and sequences.
The evidence applies to those requests, not all possible inputs or a whole backend.
Unsupported HTTP features such as streaming and arbitrary response headers need
another profile. A failed rerun invalidates previous evidence.

The `tests/fixtures` projects are synthetic fixtures, with synthetic tokens and
empty disposable databases. They are not customer snapshots. Real GitHub pilot
assessments and runnable examples belong to `sanka-examples`.
