# Python enum classifier to Jev

An offline **Sanka Code** extension for one reviewed Python OpenAI Responses
classifier shape. It parses source without importing it and binds a reviewed
question and fixed Choice options to the exact source file and call site. The
application owns Jev configuration, credentials, dependencies, inference and rollout.

See [CONTRACT.md](CONTRACT.md) for the source support matrix, decision schema and
module interfaces. The synthetic positive fixture returns `billing`, `technical`,
`sales` or `unknown`, and already returns `unknown` on exceptions. Version one
preserves that reviewed fallback; it does not translate confidence thresholds.

Nested source packages, reflection/monkeypatching, aliases, dynamic schemas,
wrappers, asynchronous code, explanations, tools,
multimodal input, conversation state and multiple output fields need manual work.
Unknown or unsupported call sites remain in the inventory and candidate. A green
compatibility check establishes only the old input/output contract, never equivalent
model behavior, accuracy, savings or latency. Live evaluation is a separate,
explicit application-owned action and is never part of the converter lifecycle.

`extension.template.json` is an **unpublished template**, not an installable
marketplace manifest. Its empty wheel closure must be populated with built,
reviewed immutable wheels and real SHA-256 hashes by the acceptance/release harness.
Do not publish the template or invent wheel hashes. Commands are advertised only
when their implementation is integrated and tested. No new CLI command or SDK
abstraction is introduced; consumers use top-level scan, plan, apply, test, verify.

The converter depends only on `sanka-extension-sdk==0.1.0a4`; generated destination
code owns `typesafe-sdk==0.7.0`. No provider SDK is imported by the converter.
