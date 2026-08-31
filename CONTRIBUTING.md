# Contributing to Sanka Extensions

Thank you for improving Sanka Extensions. Contributions may add or improve
stack-specific migration support for frameworks, databases, languages,
libraries, and files. Before opening a pull request, read [The extension
model](docs/extensions.md), the current [connector-extension
boundaries](docs/connector-development.md), and the [Security
Policy](SECURITY.md).

Use Python 3.12 and `uv`, keep shared SDKs dependency-free, and put each extension's
optional dependencies in that extension package only. A new extension kind must first add a
typed, versioned interface and boundary validation; do not couple extension code to
the AGPL runtime. Hosted SaaS and managed-system connectors do not belong in
this repository.

```bash
uv sync --frozen --all-packages
make check
```

Please add focused regression tests with behavior changes and update public
documentation when an extension contract changes. Report suspected
vulnerabilities privately as described in `SECURITY.md`.

By contributing, you agree that your contribution is licensed under the
Apache License 2.0 in this repository.
