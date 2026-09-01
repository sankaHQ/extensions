# Sanka DRF-to-FastAPI extension

This independently executable extension scans Django REST Framework projects and plans,
generates, tests, and verifies FastAPI migrations through the `sanka-extension/v1` JSON
subprocess contract.

It writes one JSON response to stdout. Diagnostics are written to stderr.
