# Extension catalog

Extensions are grouped by what they migrate: **Data**, **Workflow**, or **Code**.
Database engines, file formats and frameworks describe what an extension works with.
Hosting a database yourself or using a managed service does not change its category.

The available packages and their supported operations are generated from
`marketplace.json` and the extension manifests.

## Data

Read or write records and content in configured data endpoints.
An endpoint can be a database, a file or a directory of files.
Readers supply migration sources; writers supply migration destinations.

| Extension | Data endpoint type | Reads | Writes |
| --- | --- | --- | --- |
| `sanka/clickhouse` | clickhouse | — | Yes |
| `sanka/csv` | csv | Yes | — |
| `sanka/markdown` | markdown | Yes | — |
| `sanka/postgres` | postgres | Yes | Yes |
| `sanka/sqlite` | sqlite | Yes | Yes |

## Workflow

Migrate or reconstruct automations, triggers, actions and conditions.

There are no executable Workflow extensions in this marketplace yet.
The SDK provides the [Flow definition contract](flow.md); creating a definition
does not construct or activate a workflow.

## Code

Convert application code between frameworks or other code technologies.
Each extension defines its supported source projects and conversion targets.
For example, DRF-to-FastAPI converts a Django REST Framework application to FastAPI.
Supported scopes and limitations are documented in each extension's package README.

| Extension | Conversion target |
| --- | --- |
| `sanka/drf-to-fastapi` | fastapi |
| `sanka/drf-to-flask` | flask |
