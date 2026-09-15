# DRF UUID and Boolean compatibility

The native DRF-to-FastAPI converter supports stock `UUIDField` and
`BooleanField` serializer fields backed by the corresponding Django model
fields. This removes scalar and lookup blockers for applications with UUID
identifiers and Boolean state, including shopping-list APIs.

## Supported behavior

- UUID input validation, canonical output, primary-key lookup, and read-only
  foreign keys referencing UUID primary keys.
- Exact `uuid.uuid4` model defaults generate a fresh identifier on creation.
  Constant defaults remain creation defaults; updates do not reset omitted fields.
- Boolean JSON input follows DRF's accepted values, null handling, and errors.
- Generated Tortoise and SQLAlchemy models use UUID/Boolean columns mapped to
  existing Django tables. Django's SQLite UUID storage uses 32 hex digits;
  PostgreSQL uses native UUID values.
- A primary-key detail route can set `lookup_url_kwarg`, for example `item_pk`,
  independently of another `pk` parameter in the URL. Bounded parent scoping is
  covered by the [membership and scoped-write contracts](drf-membership-compatibility.md).
- Scan schema 9 captures UUID defaults and URL aliases. Earlier scan hashes
  retain their original payload representation.

Custom field subclasses, custom UUID default callables, noncanonical UUID output
formats, unsupported validators, and UUID primary keys that are writable or omitted
from the serializer
remain outside the supported envelope. New field support does not bypass any
queryset, authentication, permission, middleware, or serializer-write checks.

## Verification

`test_uuid_boolean.py` compares scalar validation with installed DRF, checks
creation defaults and rejection boundaries, and exercises existing Django SQLite
UUID rows with the generated ORM mapping. Linux CI additionally converts a
synthetic generic CRUD application and compares HTTP responses and final database
state with the source application. The modern-framework job repeats this on
Django 6.0.6 and DRF 3.17.1.

## Shopping-list application readiness

Scalar support alone does not establish application compatibility. The subsequent
[membership and scoped-write contracts](drf-membership-compatibility.md) cover the
recognized permission/query/write patterns, including the surrounding relation display.
Token/session authentication fallback, custom throttles, pagination and middleware
still require compatible implementations or an explicitly reviewed route scope.
Full source/generated HTTP and database acceptance, an approved hosted release, and
an end-to-end GitHub pull-request run remain required before claiming completion.
