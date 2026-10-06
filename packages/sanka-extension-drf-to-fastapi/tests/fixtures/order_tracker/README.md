# order-tracker

A small Django REST Framework order-management API: `Order` and `OrderItem`
with a foreign key, a unique order reference, status choices, and decimal
prices. Ships with a seeded `db.sqlite3`, so the Sanka migration lifecycle
works immediately.

```bash
uv tool install --upgrade --python 3.12 'sanka-cli==0.3.9'
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
sanka extension marketplace list
sanka extension marketplace upgrade official
sanka extension add sanka/drf-to-fastapi

sanka scan .
sanka plan . --to python-fastapi --orm sqlalchemy --generation minimal \
  --output .sanka/output/fastapi --strategy native --package-manager uv
# Review .sanka/plan.json and use the hash printed by Plan.
sanka apply --root . --plan-hash '<reviewed-plan-hash>'
sanka test .
sanka verify .
```

Stop before Apply if Plan reports unsupported routes or manual adaptations.

`scan` records what this app contains, `plan` produces a reviewable hashed
plan, `apply` generates a FastAPI app under `.sanka/output/fastapi/` (this
source tree is never modified), `sanka test` runs unit tests against the
generated app, and `verify` compares its behavior with this app and the run
ledger.

To poke at the source app itself: `.venv/bin/python manage.py runserver` and browse
`/api/orders/`.
