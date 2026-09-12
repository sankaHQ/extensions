# Sanka Extension SDK compatibility package

This Apache-2.0 package preserves the published `sanka_connector` imports and shared data-access types used by existing extension wheels. It has no runtime dependencies.

New extensions use the [Sanka Extension SDK](../sanka-extension-sdk/README.md) and import `sanka_extensions.data`. The SDK installs this compatibility dependency automatically. See [the compatibility guide](../../docs/naming-compatibility.md) for retained identifiers and removal conditions.
