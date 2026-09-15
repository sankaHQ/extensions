# DRF membership and scoped writes

The DRF-to-FastAPI converter captures a bounded set of membership, query and write
rules as scan-schema-10 contracts. Generated handlers use async SQL; they do not
execute the source application's permission or serializer methods.

## Supported contracts

- Object permissions with a superuser short circuit followed by membership in an
  automatic many-to-many user relation, directly or through one parent foreign key.
- The equivalent view permission that looks up a parent from a URL parameter before
  checking membership. Both Django's and DRF's stock `get_object_or_404` are recognized.
- Querysets filtered by the authenticated user's membership, a URL parent value, or
  a membership-filtered parent subquery. Ordering is captured independently of the
  response fields. Object lookups retain these filters, including nested URLs whose
  parent and item parameters differ.
- A stock create followed by adding the requesting user as a member.
- Serializer create methods assigning a read-only parent from the request URL and
  rejecting an existing same-name row whose Boolean flag is false before creating.
- Member add/remove APIView PUT actions: parent lookup, request parsing, object
  permission check, validation of all member IDs, and the original add/remove/save
  loop. A source save touches its `auto_now` columns; an empty input performs no save.
- Read-only member ID/name fields and a bounded preview of related names selected
  with a Boolean filter. These preserve the surrounding list response.
- Stock direct cascading deletion of child rows and automatic member relations,
  committed with the parent deletion in one transaction. Custom delete hooks,
  signals, deeper cascades and other on-delete policies remain manual.

Only single stock TokenAuthentication is admitted for these contracts. Authentication
reads the configured user table, including custom user models. A superuser permission
bypass does not override a user-filtered queryset. Invalid tokens fail before object
lookup; object permissions run after a scoped lookup, preserving missing-object versus
forbidden responses. All values passed to SQL remain bound parameters.

## Rejection boundaries

A changed predicate, extra statement, custom validation/write hook, custom M2M through
model, signal, unsupported manager, or unsupported authentication combination is not
silently ignored. The affected route remains manual. The contracts are not a general
Python transpiler or a claim that arbitrary DRF customizations are supported.

SessionAuthentication fallback, custom throttle classes, PageNumberPagination and
unsupported middleware retain their separate compatibility gates. In particular,
adding these contracts does **not** establish full compatibility for the shopping-list
application while those gates remain. Do not present a partial plan as a completed
application migration or ask a user to fund a retry before its remaining gaps are resolved.

## Verification

`test_access_contracts.py` uses an original synthetic collection/entry application
with a custom user model. It compares real Django responses and final database state
with the generated request layer over Tortoise and SQLAlchemy SQLite stores. Cases
cover members, outsiders, superusers, anonymous callers, missing objects, cross-parent
lookups, creator membership, duplicate checks, invalid member IDs, add/remove loops,
self-removal, member displays, related previews and cascading deletion. Negative source
shapes must remain unsupported. The tests do not copy a customer's repository.

Linux CI additionally runs the complete scan/plan/apply pipeline and repeats request
and database comparisons against the generated application. The modern-framework job
repeats these checks on Django 6.0.6 and DRF 3.17.1. Publication requires the separate
exact-commit converter regression check and human approval of the final PR head.
