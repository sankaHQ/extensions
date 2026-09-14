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
  independently of another `pk` parameter in the URL. This does not implement
  parent scoping; a custom queryset remains unsupported until its behavior can
  be lowered and verified.
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

## Remaining work for the shopping-list application

This change alone does not establish full application compatibility. The following
behaviors need typed conversion contracts and source-versus-target acceptance
coverage before a complete migration can be offered:

1. Authenticated-user and URL-parameter queryset filters, stable ordering, and
   parent-scoped detail lookups. Test cross-parent and cross-user isolation.
2. Membership permissions (including the superuser rule), token/session
   authentication order, and permission checks before reads and writes.
3. Member relations, computed serializer fields, parent assignment, duplicate
   checks, and membership add/remove operations with matching transaction behavior.
4. Custom throttle scopes, middleware, and authentication/schema endpoints, or an
   explicitly reviewed route scope that excludes unsupported endpoints without
   representing the entire application as migrated.
5. Complete source and generated HTTP/database comparison in isolated CI, followed
   by an approved hosted release and an end-to-end GitHub pull-request run.

The hosted API continues rejecting plans with no generatable routes before it
reserves credits. Adding scalar support is not evidence that the complete
shopping-list application is ready for a paid retry.
