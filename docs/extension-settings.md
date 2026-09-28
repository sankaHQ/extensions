# Extension plan settings

A code extension can ship `sanka-extension-settings.json` inside its Python package.
It declares the settings a person can choose before a stage runs, so the Sanka CLI
TUI and Sanka Code render the same form from one declaration. The file lives in the
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

The file declares what the extension accepts, not hosting policy. Sanka Code decides
separately which settings are editable, fixed or hidden in the cloud.
`tests/test_extension_settings.py` validates every shipped file.
