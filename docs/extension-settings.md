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
for these lists; nested objects/arrays are invalid. CLI 0.3.9 supports these lists.

The file declares what the extension accepts, not hosting policy. Sanka Code decides
separately which settings are editable, fixed or hidden in the cloud.
`tests/test_extension_settings.py` validates every shipped file.

Python-to-Go `0.1.0a17` detects the source framework, entrypoint, models and
source database during Scan. Plan offers three destination choices: same as
source, PostgreSQL (`pgx`), or SQLite (`sqlite`). Stateless sources still resolve
to `none`; explicit or saved `none` configurations remain supported, but it is
not a menu choice. Source overrides are in Advanced configuration. Schema mode
appears for PostgreSQL; models-file overrides apply to auto, PostgreSQL and SQLite.

Select the Go router with `sanka plan .` or explicit `--to go-chi`, `go-fiber`,
`go-mux` or `go-gin`. CLI 0.3.9 and its optional TUI read the same installed
settings declaration. Changing settings requires a new reviewed Plan. Output
remains inside the artifact directory; there is no generation-layout setting.
