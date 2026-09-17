// SPDX-License-Identifier: Apache-2.0
// Bounded TypeScript syntax driver. It parses or transpiles the texts it receives on
// standard input. It never resolves imports, reads other files or executes them.
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ts = require(path.join(__dirname, "typescript.js"));

const MAX_INPUT_BYTES = 16 * 1024 * 1024;
const MAX_DEPTH = 512;
const K = ts.SyntaxKind;
// SyntaxKind is a numeric enum with range aliases (FirstStatement, LastToken, ...).
// Reverse lookups must name the canonical member, never an alias.
const KIND_NAMES = {};
for (const [name, value] of Object.entries(K)) {
  if (typeof value !== "number") continue;
  const alias = /^(First|Last)[A-Z]/.test(name);
  const current = KIND_NAMES[value];
  if (current === undefined || (!alias && /^(First|Last)[A-Z]/.test(current))) {
    KIND_NAMES[value] = name;
  }
}
const SKIPPED_FIELDS = new Set([
  "parent",
  "original",
  "jsDoc",
  "jsDocCache",
  "emitNode",
  "symbol",
  "localSymbol",
  "locals",
  "nextContainer",
  "flowNode",
  "endFlowNode",
  "returnFlowNode",
  "externalModuleIndicator",
  "commonJsModuleIndicator",
  "endOfFileToken",
]);
const TEXT_KINDS = new Set([
  K.Identifier,
  K.PrivateIdentifier,
  K.StringLiteral,
  K.NumericLiteral,
  K.BigIntLiteral,
  K.RegularExpressionLiteral,
  K.NoSubstitutionTemplateLiteral,
  K.TemplateHead,
  K.TemplateMiddle,
  K.TemplateTail,
  K.JsxText,
]);
const TYPE_ONLY_KINDS = new Set([
  K.ImportClause,
  K.ImportSpecifier,
  K.ExportSpecifier,
  K.ExportDeclaration,
  K.ImportEqualsDeclaration,
]);
const SCRIPT_KINDS = {
  ".ts": ts.ScriptKind.TS,
  ".mts": ts.ScriptKind.TS,
  ".cts": ts.ScriptKind.TS,
  ".tsx": ts.ScriptKind.TSX,
  ".js": ts.ScriptKind.JS,
  ".mjs": ts.ScriptKind.JS,
  ".cjs": ts.ScriptKind.JS,
  ".jsx": ts.ScriptKind.JSX,
};

function fail(message) {
  process.stdout.write(JSON.stringify({ error: String(message) }) + "\n");
  process.exit(2);
}

function isNode(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    typeof value.kind === "number" &&
    typeof value.pos === "number" &&
    typeof value.end === "number"
  );
}

// Builds a JavaScript value from a pure literal expression. Nothing is evaluated:
// only literal tokens, arrays and object literals with literal members qualify.
function literal(node) {
  switch (node.kind) {
    case K.StringLiteral:
    case K.NoSubstitutionTemplateLiteral:
      return { ok: true, value: node.text };
    case K.NumericLiteral: {
      const value = Number(node.text);
      return Number.isFinite(value) ? { ok: true, value } : { ok: false };
    }
    case K.TrueKeyword:
      return { ok: true, value: true };
    case K.FalseKeyword:
      return { ok: true, value: false };
    case K.NullKeyword:
      return { ok: true, value: null };
    case K.ParenthesizedExpression:
      return literal(node.expression);
    case K.PrefixUnaryExpression: {
      if (node.operator !== K.MinusToken || node.operand.kind !== K.NumericLiteral) {
        return { ok: false };
      }
      const inner = literal(node.operand);
      return inner.ok ? { ok: true, value: -inner.value } : inner;
    }
    case K.ArrayLiteralExpression: {
      const items = [];
      for (const element of node.elements) {
        if (element.kind === K.SpreadElement || element.kind === K.OmittedExpression) {
          return { ok: false };
        }
        const inner = literal(element);
        if (!inner.ok) return inner;
        items.push(inner.value);
      }
      return { ok: true, value: items };
    }
    case K.ObjectLiteralExpression: {
      const value = {};
      const seen = new Set();
      for (const property of node.properties) {
        if (property.kind !== K.PropertyAssignment) return { ok: false };
        const name = property.name;
        let key;
        if (name.kind === K.Identifier) key = name.text;
        else if (name.kind === K.StringLiteral || name.kind === K.NoSubstitutionTemplateLiteral) {
          key = name.text;
        } else if (name.kind === K.NumericLiteral) key = String(Number(name.text));
        else return { ok: false };
        if (seen.has(key) || key === "__proto__") return { ok: false };
        seen.add(key);
        const inner = literal(property.initializer);
        if (!inner.ok) return inner;
        value[key] = inner.value;
      }
      return { ok: true, value };
    }
    default:
      return { ok: false };
  }
}

function tree(node, sourceFile, depth) {
  if (depth > MAX_DEPTH) throw new Error("source nesting exceeds the driver limit");
  const out = { k: KIND_NAMES[node.kind], s: node.getStart(sourceFile), e: node.end };
  if (TEXT_KINDS.has(node.kind) && typeof node.text === "string") out.t = node.text;
  if (node.kind === K.PrefixUnaryExpression || node.kind === K.PostfixUnaryExpression) {
    out.op = KIND_NAMES[node.operator];
  }
  if (node.kind === K.VariableDeclarationList) {
    const flags = node.flags;
    if ((flags & ts.NodeFlags.AwaitUsing) === ts.NodeFlags.AwaitUsing) out.decl = "await using";
    else if (flags & ts.NodeFlags.Using) out.decl = "using";
    else if (flags & ts.NodeFlags.Const) out.decl = "const";
    else if (flags & ts.NodeFlags.Let) out.decl = "let";
    else out.decl = "var";
  }
  if (TYPE_ONLY_KINDS.has(node.kind)) {
    if (node.isTypeOnly === true || node.phaseModifier === K.TypeKeyword) out.typeOnly = true;
  }
  if (node.kind === K.ExportAssignment && node.isExportEquals) out.exportEquals = true;
  if (node.kind === K.ObjectLiteralExpression || node.kind === K.ArrayLiteralExpression) {
    const value = literal(node);
    if (value.ok) out.json = JSON.stringify(value.value);
  }
  const fields = {};
  for (const [name, value] of Object.entries(node)) {
    if (SKIPPED_FIELDS.has(name)) continue;
    if (isNode(value)) {
      fields[name] = tree(value, sourceFile, depth + 1);
    } else if (Array.isArray(value) && typeof value.pos === "number") {
      if (!value.every(isNode)) continue;
      fields[name] = value.map((item) => tree(item, sourceFile, depth + 1));
    }
  }
  if (Object.keys(fields).length) out.f = fields;
  return out;
}

function scriptKind(file) {
  const extension = path.posix.extname(file.path).toLowerCase();
  const kind = SCRIPT_KINDS[extension];
  if (kind === undefined) throw new Error(`${file.path}: unsupported file extension`);
  return kind;
}

function diagnostics(list) {
  return (list || []).map((diagnostic) => ({
    code: diagnostic.code,
    message: ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
    start: typeof diagnostic.start === "number" ? diagnostic.start : null,
    length: typeof diagnostic.length === "number" ? diagnostic.length : null,
  }));
}

function parseFile(file) {
  const kind = scriptKind(file);
  const sourceFile = ts.createSourceFile(file.path, file.text, ts.ScriptTarget.Latest, false, kind);
  return {
    path: file.path,
    kind: ts.ScriptKind[kind].toLowerCase(),
    diagnostics: diagnostics(sourceFile.parseDiagnostics),
    tree: tree(sourceFile, sourceFile, 0),
  };
}

function transpileFile(file) {
  scriptKind(file);
  const result = ts.transpileModule(file.text, {
    fileName: file.path,
    reportDiagnostics: true,
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
      jsx: ts.JsxEmit.React,
      sourceMap: false,
      inlineSourceMap: false,
      removeComments: false,
    },
  });
  return { path: file.path, output: result.outputText, diagnostics: diagnostics(result.diagnostics) };
}

function main() {
  const raw = fs.readFileSync(0);
  if (raw.length > MAX_INPUT_BYTES) return fail("input exceeds the driver limit");
  let request;
  try {
    request = JSON.parse(raw.toString("utf8"));
  } catch {
    return fail("invalid driver request");
  }
  if (!request || typeof request !== "object" || !Array.isArray(request.files)) {
    return fail("invalid driver request");
  }
  const files = [];
  for (const file of request.files) {
    if (!file || typeof file.path !== "string" || !file.path || typeof file.text !== "string") {
      return fail("invalid driver file");
    }
    files.push({ path: file.path, text: file.text });
  }
  let result;
  try {
    if (request.command === "parse") {
      result = { typescript: ts.version, files: files.map(parseFile) };
    } else if (request.command === "transpile") {
      result = { typescript: ts.version, files: files.map(transpileFile) };
    } else {
      return fail("unsupported driver command");
    }
  } catch (error) {
    return fail(error && error.message ? error.message : error);
  }
  process.stdout.write(JSON.stringify(result) + "\n");
  return undefined;
}

main();
