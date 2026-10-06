# order-tracker

A small Django REST Framework order-management API: `Order` and `OrderItem`
with a foreign key, a unique order reference, status choices, and decimal
prices. Ships with a seeded `db.sqlite3`, so the Sanka migration lifecycle
works immediately.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt sanka-cli "sqlalchemy[asyncio]" httpx uvicorn

sanka scan .
sanka plan . --to fastapi --orm sqlalchemy
sanka apply --to fastapi
sanka test
sanka verify --to fastapi
```

`scan` records what this app contains, `plan` produces a reviewable hashed
plan, `apply` generates a FastAPI app under `.sanka/output/fastapi/` (this
source tree is never modified), `sanka test` runs unit tests against the
generated app, and `verify` compares its behavior with this app and the run
ledger.

To poke at the source app itself: `python manage.py runserver` and browse
`/api/orders/`.
