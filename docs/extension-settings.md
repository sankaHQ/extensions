# Extension plan settings

A code extension can ship `sanka-extension-settings.json` inside its Python package.
It declares settings before a stage runs. The CLI uses their defaults and input
hints; the optional TUI and Sanka Code render forms from the same declaration. The file lives in the
wheel rather than in `extension.json` because installed CLIs accept only the exact
manifest keys of `sanka-extension-manifest/v2`; package data reaches them without a
manifest change. A consumer that finds no file keeps its built-in form.

```json
{
  "schema_version": "sanka-extension-settings/v1",
  "display": {"name": {"en": "DRF to FastAPI", "ja": "DRF から FastAPI"}},
  "settings": [
    {
      "id": "swagger_ui",
      "stage": "plan",
      "type": "boolean",
      "default": true,
      "label": {"en": "Swagger UI at /docs", "ja": "/docs の Swagger UI"}
    }
  ]
}
```

Each setting has an `id` (the extension configuration key), a `stage`, a `type`
(`choice`, `boolean`, `integer`, `text` or `path`), a `default` and an English and
Japanese `label`. `description` is optional. Choices list `value` and `label`; the
default must be one of them. Integers declare `minimum` and `maximum`. Text and path
settings may be `optional` with a `null` default. `advanced` places a setting under
Advanced. `when` shows a setting only while other settings hold the given values.
An updated CLI also accepts a nonempty list of scalar alternatives, such as
`"when": {"database_layer": ["pgx", "sqlite"]}`. Consumers must use membership
for these lists; nested objects/arrays are invalid. This addition is unreleased.

The file declares what the extension accepts, not hosting policy. Sanka Code decides
separately which settings are editable, fixed or hidden in the cloud.
`tests/test_extension_settings.py` validates every shipped file.

Python-to-Go a11 declares its source framework, source entrypoint and
optional PostgreSQL conversion settings. Schema mode and models file appear only
when PostgreSQL is selected. Output remains inside the artifact directory; select
the Go router with CLI `--to fiber` (or `chi`, `mux`, `gin`) or the optional TUI target list. There
is no generation-layout setting. Changing these values requires a new reviewed
plan. CLI versions that support settings declarations read it from the installed wheel.
Public CLI 0.3.3 keeps its built-in form; it does not read this declaration.
