# Contributing to Sanka Mods

Thank you for improving Sanka Mods. Contributions may add or improve
stack-specific migration support for frameworks, databases, languages,
libraries, and files. Before opening a pull request, read [The mod
model](docs/mods.md), the current [connector-mod
boundaries](docs/connector-development.md), and the [Security
Policy](SECURITY.md).

Use Python 3.12 and `uv`, keep shared SDKs dependency-free, and put each mod's
optional dependencies in that mod package only. A new mod kind must first add a
typed, versioned interface and boundary validation; do not couple mod code to
the AGPL runtime. Hosted SaaS and managed-system connectors do not belong in
this repository.

```bash
uv sync --frozen --all-packages
make check
```

Please add focused regression tests with behavior changes and update public
documentation when a mod contract changes. Report suspected
vulnerabilities privately as described in `SECURITY.md`.

By contributing, you agree that your contribution is licensed under the
Apache License 2.0 in this repository.
