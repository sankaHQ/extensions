# Sanka TypeScript capture

Shared syntax driver for code extensions that read TypeScript or JavaScript
sources (currently `sanka/typescript-to-rust`; React Native capture will reuse it).
It is a helper library, not an executable extension or an SDK, and it has no
Python dependencies.

The package ships the TypeScript compiler bundle (`typescript@5.9.3`,
`lib/typescript.js`, Apache-2.0) and a small Node.js driver. The 9 MB bundle is
not committed: `scripts/fetch_typescript_bundle.py` downloads the pinned npm
tarball, verifies the tarball and bundle digests, and places the file (git-ignored);
`make check` runs it, and built wheels include the bundle so installed extensions
never touch the network. Every invocation
checks the bundle digest, then runs `node` with a cleared environment on exactly
the texts it is given. The driver parses (`ts.createSourceFile`) or transpiles
(`ts.transpileModule`) those texts. It never resolves imports, reads other files,
downloads packages or executes project code.

Node.js 20 or later is required on `PATH`, or set `SANKA_NODE` to an executable.
The parse output does not depend on the Node.js version: the vendored compiler
decides the syntax tree and the JavaScript number and string serialization used
for literal payloads.

```python
from sanka_ts_capture import parse_sources, tree

parsed = parse_sources({"src/app.ts": text})["src/app.ts"]
for statement in tree.field_list(parsed.tree, "statements"):
    print(tree.kind(statement))
```

Each node is a plain dictionary: `k` (SyntaxKind name), `s`/`e` (offsets), `t`
(identifier or literal text), `f` (TypeScript property name → child node or node
list), `op` (unary operator), `decl` (`const`, `let`, `var`, `using`), `typeOnly`,
`exportEquals`, and `json` (the exact `JSON.stringify` output of a pure literal
object or array). Syntax diagnostics are returned, not raised; callers decide
whether a file with diagnostics is a gap.

Limits: 512 KiB per file, 4 MiB per request, 64 MiB of driver output, nesting
depth 512, and a 120-second timeout. Exceeding any limit is an error.
