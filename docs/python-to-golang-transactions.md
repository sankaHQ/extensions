# Python-to-Golang transaction contract

This is the qualification contract for the first single-database write profile. Fiber, chi, mux,
and Gin use it for the bounded flat-model POST/PUT/PATCH/DELETE recipe documented by the extension.
It does not qualify arbitrary handlers, broader validation, or relationship writes. Existing
read-only generated projects remain unchanged.

Use the pinned pgx `BeginFunc` primitive for a single unit of work. All statements
in that unit must use the callback transaction and receive the operation context.
Do not write through the pool from inside the callback, retain the transaction
past callback completion, recover a panic as success, or retry automatically.
Only a successful commit permits a success response. Validate source-specific
input and authorization before entering the unit when the source does so.

| Event | Required behavior |
| --- | --- |
| Begin fails or context is already cancelled | Return failure; do not run the callback |
| Callback succeeds | Commit once; return success only if commit succeeds |
| Callback returns an error | Roll back; preserve the original operation error |
| Callback panics | Roll back and propagate the panic to the HTTP recovery boundary |
| Operation context is cancelled or its deadline expires | Return failure; clean up the transaction and release or discard its connection |
| Commit fails | Return failure; never infer that nothing was written from a transport error |
| Rollback fails | Never report success; the connection must not be reused in a dirty transaction |
| Any failure | No implicit retry; subsequent operations must be able to acquire the pool |

A PostgreSQL error rejecting a deferred constraint proves that particular commit
rolled back. A disconnected client or lost commit acknowledgement does **not**
prove rollback. Such outcomes remain unknown; future write replay must observe
rows independently, and retry support requires separately captured idempotency
semantics. Sequence gaps after rollback are permitted; identifiers are not
rewound or reused by this contract.

Source validation status codes, error envelopes, authorization, distributed
transactions, nested savepoints, side effects and request cancellation wiring
remain separately qualified work. In particular, these tests do not assert that
Fiber cancellation follows net/http client-disconnect behavior.

## Executable qualification

`packages/sanka-extension-python-to-golang/tests/test_golang_transactions.py`
uses the generated Fiber/PostgreSQL lock and the exact pinned pgx implementation.
Native fault injection checks begin, callback, rollback and commit failures,
panic propagation and lack of retries. The explicit PostgreSQL fixture compares
Django `transaction.atomic` (DRF) and SQLAlchemy transaction scopes (Flask and
FastAPI) with pgx for successful writes, application exceptions, constraint
failures and errors raised only at commit. Go-only cases cover cancellation and
an expired operation context. A single-connection pool and a bounded follow-up
write check cleanup after each case.

Source and target get separate random schemas on the explicitly configured test
server, removed in `finally`. The deferred foreign-key table is a test fixture;
its use does not advertise foreign-key capture or migration support.
No listening service or customer migration is started by the tests.

```bash
SANKA_GO_TESTS=1 uv run python -m pytest \
  packages/sanka-extension-python-to-golang/tests/test_golang_transactions.py -q
```

Database cases additionally require `SANKA_MIGRATE_TEST_POSTGRES_DSN` pointing to
a disposable test database. CI already provides it in the Python-to-Golang lane.
Without that explicit fixture, the three database cases skip; a native fault-test
pass alone is not database-parity evidence.
