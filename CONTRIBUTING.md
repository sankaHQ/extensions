# Contributing to Sanka Connectors

Thank you for improving Sanka Connectors. Before opening a pull request, read
the provider boundaries in [Connector development](docs/connector-development.md)
and the supported trust model in [Security Policy](SECURITY.md).

Use Python 3.12 and `uv`, keep the Connector SDK dependency-free, and put each
provider's dependencies in that provider package only. Hosted SaaS and managed
system connectors do not belong in this repository.

```bash
uv sync --frozen --all-packages
make check
```

Please add focused regression tests with behavior changes and update public
documentation when a connector contract changes. Report suspected
vulnerabilities privately as described in `SECURITY.md`.

By contributing, you agree that your contribution is licensed under the
Apache License 2.0 in this repository.
