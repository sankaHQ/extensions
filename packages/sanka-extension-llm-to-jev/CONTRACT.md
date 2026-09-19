# Frozen v1 interface

Schema: `src/sanka_extension_llm_to_jev/schemas/decision-v1.json`.
Example: `tests/fixtures/positive/jev-decision.json`. Project default is
`jev-decision.json`; request configuration may set `decision_spec` to a relative
path. One reviewed call per specification. The source tree is never executed by scan.

Modules (ordinary JSON dictionaries, Paths):

- `scanner.scan_project(root: Path) -> dict`: `schema_version`, `source_digest`,
  `files` (relative path -> SHA256), `call_sites`, `manual_count`.
- Call site: `id` = `<file>:<qualified_function>:<line>`, `file`, `function`,
  `line`, `end_line`, `function_line`, `function_end_line`, `source_hash`,
  `status` (`supported`/`manual`), `reasons`; supported sites additionally
  `input_name`, `enum_field`, `labels`, `unknown_label`, `prompt`, `model`.
- `decision.validate_spec(spec, inventory) -> dict`: strict schema plus source
  and call binding; returns same validated dictionary.
- `generator.render_candidate(root, spec, inventory) -> dict[str, str]`: changed
  and added UTF-8 files only; original files copied by apply. Never writes itself.
- `planning.build_plan(root, spec, inventory, manifest_digest) -> dict` calls
  generator. Plan contains `schema_version`, `bindings`, `decision`, `call_site`,
  `changed_files` (path -> full contents), `generated_digests`, `manual_call_sites`,
  `dependencies`, `behavioral_differences`, `diff`, `plan_hash` (canonical hash
  of all other fields). Bindings: `source_digest`, `decision_digest`,
  `inventory_digest`, `extension_digest` (manifest), `extension_id`,
  `extension_version`. Generated digests cover changed files; source files
  inventory covers all preserved files. Manual sites stay present in reports.
- `lifecycle.handle(request) -> ExtensionResponse` owns apply/test/verify. Adapter
  delegates to it; A owns scan/plan and transport. `configuration.extension_plan_hash`
  is extension plan hash; `reviewed_plan_hash` belongs to CLI and is different.
  Prior artifacts contain `migration-plan.json`; candidate lives under artifact root.

Supported source grammar is the exact positive fixture in a project-root Python file
(nested package import layouts require manual review): module `import json`,
`from openai import OpenAI`, `client = OpenAI()`; one undecorated sync function,
one annotated `text: str` argument and `-> str`; try body exactly response assignment
plus JSON enum field return, except Exception exactly literal unknown return.
Only request keywords model/instructions/input/text, all except input literals.
Text uses strict inline JSON schema with one required string enum, no extra fields.
No source aliases, client factories, extra function statements, or dynamic schemas.

All exceptions already map to the reviewed unknown label in the source. Confidence
threshold is uncalibrated and application-owned, never mechanically translated.
The v1 target is a fixed Choice with string IDs matching source labels exactly.
