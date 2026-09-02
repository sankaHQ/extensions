# Sanka DRF-to-FastAPI extension

This independently executable extension scans Django REST Framework projects and plans,
generates, tests, and verifies FastAPI migrations through the `sanka-extension/v1` JSON
subprocess contract.

It writes one JSON response to stdout. Diagnostics are written to stderr.

## Parity notes

Every scanned route carries `parity_notes`: facts about the exact behavior of the
source application that a port must reproduce, derived from the live Django and DRF
classes rather than guessed. Families: `routing` (allowed methods, 405/OPTIONS bodies,
missing-object and trailing-slash behavior), `auth` (authenticator order, 401/403/404
ordering, exact token/session/basic failure details, CSRF), `conditional` (ETag and
304/412 logic and the operations that call it), `pagination`, `ordering`, `filtering`,
`multipart` (parsers and file-field rules), `uniqueness`, `nullability`, `messages`
(exact per-field validation strings), and `overrides` (project methods and the literal
strings they emit). Notes appear in `.sanka/scan.json` (schema 5), `plan-*.json`
(schema 4), the gap report, and the generated manifest's `unsupported_routes`. A family
that cannot be derived reports `SANKA_DRF_PARITY_UNAVAILABLE` instead of failing the scan.

## OPTIONS and 405 parity

Generated native apps answer `OPTIONS` with the exact `SimpleMetadata` body DRF would
send — view name, description, renderer and parser media types, and the `actions`
field map for POST/PUT when the caller passes the permission checks (PUT only when the
object exists and, for owner-restricted views, belongs to the caller). The scan captures
the anonymous and the authorized variants from the installed DRF; the runtime chooses
between them per request. Unsupported methods answer DRF's `405 {"detail": "Method
\"X\" not allowed."}` with the `Allow` header in `http_method_names` order.

## List semantics and datetime fields

The native envelope covers DRF's generic list machinery: `CursorPagination` subclasses
that only set attributes (page size, ordering, parameter names; cursors, `next`/`previous`
links and the "Invalid cursor" 404 are reproduced statement for statement), `SearchFilter`
over text fields with the default, `^` and `=` lookups and DRF's smart term splitting, and
`OrderingFilter` with `ordering_fields` validation and the view's default ordering. A custom
`OrderingFilter` is accepted when probing its `get_ordering` against the stock filter
identifies a known tie-break idiom (appending the primary key). `DateTimeField` (ISO 8601
in and out, timezone-aware per the project's `TIME_ZONE`) is a native field kind. An
overridden viewset action now keeps only that route manual; the rest of the viewset stays
native.

## Verbatim carryover of overridden actions

An overridden viewset action (`list`, `create`, `retrieve`, `update`, `partial_update`,
`destroy`) and the helpers it calls on `self` are carried into the generated app unchanged
when they touch nothing but the standard library, DRF's `Response`/`status`/`Request`
names, and `super().<action>()`. They are emitted as a subclass of the runtime's
`CarryoverView` in `sanka_user_views.py`: `super()` reaches the generated handler (run on
the event loop while the carried code runs in a worker thread), DRF names resolve to shims,
and an error answered by the native handler surfaces as an exception exactly where DRF's
mixins would have raised. Actions that reach the ORM, `self.request`, `get_object`, or
other imports stay manual with their reasons. Generated apps also mirror Django's
Content-Length behaviour: the header is sent only when the source ran CommonMiddleware.
