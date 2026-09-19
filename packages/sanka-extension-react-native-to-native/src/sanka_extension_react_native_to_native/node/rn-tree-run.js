// SPDX-License-Identifier: Apache-2.0
// Renders the transpiled React Native source screens with react-test-renderer against
// the in-repo stubs and writes the normalized tree of every scenario. Navigation hooks
// record typed intents; wrapped useState setters record state assignments. No network,
// no TCP listener, no real react-native package.
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");

const { createStubs, IMAGE_EXTENSIONS } = require("./react-native-stub.js");

const INTERACTIVE = { button: "press", switch: "toggle", textfield: "type" };
const HANDLERS = { press: "onPress", toggle: "onValueChange", type: "onChangeText" };

function clone(value) {
  if (value === undefined) return null;
  return JSON.parse(JSON.stringify(value));
}

function usage() {
  console.error("usage: rn-tree-run.js <spec.json> <destination.json>");
  process.exit(2);
}

function wrapReact(React, recorder) {
  const wrapped = Object.assign({}, React);
  wrapped.useState = function useState(initial) {
    const index = recorder.cursor;
    recorder.cursor += 1;
    const [value, setValue] = React.useState(initial);
    const setter = (next) => {
      if (typeof next === "function") {
        throw new Error("functional state updates are outside the captured envelope");
      }
      const name = recorder.stateNames[index];
      if (name === undefined) {
        throw new Error(`useState call ${index} has no captured state name`);
      }
      recorder.record({ set: name, value: clone(next) });
      setValue(next);
    };
    return [value, setter];
  };
  wrapped.default = wrapped;
  return wrapped;
}

function installLoader(sourceRoot, stubModules, wrappedReact) {
  const originalLoad = Module._load;
  Module._load = function load(request, parent) {
    const fromSource =
      parent && typeof parent.filename === "string" && parent.filename.startsWith(sourceRoot + path.sep);
    if (fromSource) {
      if (request === "react") return wrappedReact;
      if (Object.prototype.hasOwnProperty.call(stubModules, request)) return stubModules[request];
      if (IMAGE_EXTENSIONS.test(request)) {
        const absolute = path.resolve(path.dirname(parent.filename), request);
        const relative = path.relative(sourceRoot, absolute).split(path.sep).join("/");
        if (relative.startsWith("..")) throw new Error(`asset ${request} escapes the project`);
        return { asset: relative };
      }
      if (!request.startsWith(".")) {
        throw new Error(`screen imports ${request}, which is outside the captured envelope`);
      }
    }
    return originalLoad.apply(this, arguments);
  };
}

function sourceText(source) {
  if (source && typeof source === "object") {
    if (typeof source.asset === "string") return source.asset;
    if (typeof source.uri === "string") return source.uri;
  }
  throw new Error("image sources must be a required asset or { uri }");
}

function fromHost(element, roles) {
  if (typeof element !== "object" || element === null) {
    throw new Error("text outside <Text> is not rendered by React Native");
  }
  const role = roles[element.type];
  if (!role) throw new Error(`unsupported host element ${element.type}`);
  const props = element.props || {};
  const kids = element.children || [];
  const label = typeof props.accessibilityLabel === "string" ? props.accessibilityLabel : null;
  const node = {
    role,
    text: null,
    label,
    enabled: null,
    checked: null,
    action: null,
    children: [],
    _props: props,
  };
  switch (role) {
    case "text":
      node.text = kids
        .map((child) => {
          if (typeof child !== "string") {
            throw new Error("nested elements inside <Text> are outside the envelope");
          }
          return child;
        })
        .join("");
      break;
    case "image":
      node.text = sourceText(props.source);
      break;
    case "button":
      node.enabled = !props.disabled;
      node.children = kids.map((child) => fromHost(child, roles));
      break;
    case "textfield":
      node.text = props.value === undefined || props.value === null ? "" : String(props.value);
      if (label === null && typeof props.placeholder === "string") node.label = props.placeholder;
      break;
    case "switch":
      node.checked = Boolean(props.value);
      break;
    case "list":
      node.children = kids.map((item) => {
        if (typeof item !== "object" || item === null || item.type !== "ListItem") {
          throw new Error("FlatList children must be rendered items");
        }
        return {
          role: "listitem",
          text: item.props.itemKey,
          label: null,
          enabled: null,
          checked: null,
          action: null,
          children: (item.children || []).map((child) => fromHost(child, roles)),
          _props: {},
        };
      });
      break;
    case "indicator":
      break;
    default:
      node.children = kids.map((child) => fromHost(child, roles));
  }
  return node;
}

function normalize(json, roles) {
  if (Array.isArray(json)) {
    return {
      role: "container",
      text: null,
      label: null,
      enabled: null,
      checked: null,
      action: null,
      children: json.map((child) => fromHost(child, roles)),
      _props: {},
    };
  }
  if (json === null) throw new Error("the screen rendered nothing");
  return fromHost(json, roles);
}

function strip(node) {
  return {
    role: node.role,
    text: node.text,
    label: node.label,
    enabled: node.enabled,
    checked: node.checked,
    action: node.action,
    children: node.children.map(strip),
  };
}

function interactive(node, prefix, found) {
  if (INTERACTIVE[node.role]) found.push({ path: prefix, node });
  node.children.forEach((child, index) => interactive(child, [...prefix, index], found));
  return found;
}

function locate(tree, indexes) {
  let node = tree;
  for (const index of indexes) {
    node = node.children[index];
    if (!node) throw new Error(`no node at path ${indexes.join(".")}`);
  }
  return node;
}

function main() {
  const [specPath, destination] = process.argv.slice(2);
  if (!specPath || !destination) usage();
  const spec = JSON.parse(fs.readFileSync(specPath, "utf8"));
  const sourceRoot = fs.realpathSync(path.resolve(spec.source_root));
  const modules = fs.realpathSync(path.resolve(spec.modules));
  const React = require(require.resolve("react", { paths: [modules] }));
  const TestRenderer = require(require.resolve("react-test-renderer", { paths: [modules] }));
  const act = React.act;
  if (typeof act !== "function") throw new Error("React.act is required (React 19)");
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const originalError = console.error;
  console.error = (...args) => {
    if (typeof args[0] === "string" && args[0].includes("react-test-renderer is deprecated")) return;
    originalError(...args);
  };

  const recorder = {
    cursor: 0,
    stateNames: [],
    params: {},
    routes: Array.isArray(spec.routes) ? spec.routes : [],
    intents: [],
    record(intent) {
      this.intents.push(clone(intent));
    },
  };
  const stubs = createStubs(React, recorder);
  const wrappedReact = wrapReact(React, recorder);
  installLoader(sourceRoot, stubs.modules, wrappedReact);
  globalThis.React = wrappedReact;
  const roles = spec.roles;
  const probeText = spec.probe_text;

  const documents = [];
  for (const screen of spec.screens) {
    const loaded = require(path.join(sourceRoot, screen.file));
    const Component = loaded && loaded.__esModule && loaded.default ? loaded.default : loaded;
    if (typeof Component !== "function") {
      throw new Error(`${screen.file} does not default-export a function component`);
    }
    const props =
      screen.navigation === "react-navigation"
        ? { navigation: stubs.navigation, route: { params: screen.params } }
        : {};
    function Harness() {
      recorder.cursor = 0;
      return Component(props);
    }

    const mount = () => {
      recorder.stateNames = screen.state;
      recorder.params = screen.params;
      let renderer;
      act(() => {
        renderer = TestRenderer.create(React.createElement(Harness));
      });
      return renderer;
    };
    const unmount = (renderer) => {
      act(() => renderer.unmount());
    };
    const activate = (node, kind) => {
      const handler = node._props[HANDLERS[kind]];
      if (typeof handler !== "function") return null;
      let probe;
      if (kind === "toggle") probe = !node.checked;
      else if (kind === "type") probe = probeText;
      act(() => {
        if (kind === "press") handler();
        else handler(probe);
      });
      return probe;
    };
    const apply = (renderer, steps) => {
      for (const step of steps) {
        const tree = normalize(renderer.toJSON(), roles);
        recorder.intents = [];
        activate(locate(tree, step.path), step.kind);
      }
    };
    const probeAction = (steps, indexes, kind) => {
      const renderer = mount();
      try {
        apply(renderer, steps);
        const tree = normalize(renderer.toJSON(), roles);
        const target = locate(tree, indexes);
        recorder.intents = [];
        if (typeof target._props[HANDLERS[kind]] !== "function") return null;
        const probe = activate(target, kind);
        return recorder.intents.map((intent) => {
          const bound =
            kind !== "press" &&
            "set" in intent &&
            JSON.stringify(intent.value) === JSON.stringify(probe);
          return bound ? { bind: intent.set } : intent;
        });
      } finally {
        unmount(renderer);
      }
    };
    const document = (scenario, steps) => {
      const renderer = mount();
      let tree;
      try {
        apply(renderer, steps);
        tree = normalize(renderer.toJSON(), roles);
      } finally {
        unmount(renderer);
      }
      for (const { path: indexes, node } of interactive(tree, [], [])) {
        node.action = probeAction(steps, indexes, INTERACTIVE[node.role]);
      }
      return { screen: screen.name, scenario, params: screen.params, tree: strip(tree) };
    };

    const initial = document("initial", []);
    documents.push(initial);
    for (const entry of interactive(initial.tree, [], [])) {
      const kind = INTERACTIVE[entry.node.role];
      const action = entry.node.action;
      if (!Array.isArray(action)) continue;
      const applies = action.some((intent) =>
        kind === "press" ? "set" in intent : "bind" in intent
      );
      if (!applies) continue;
      const name = `${kind}:${entry.path.join(".")}`;
      documents.push(document(name, [{ kind, path: entry.path }]));
    }
  }
  fs.writeFileSync(destination, JSON.stringify(documents) + "\n");
}

main();
