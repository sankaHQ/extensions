// SPDX-License-Identifier: Apache-2.0
// Renders the transpiled React Native source screens with react-test-renderer against
// the in-repo stubs and writes the normalized tree of every scenario. Navigation hooks
// record typed intents; wrapped useState setters record state assignments; a fetch mock
// and an in-memory AsyncStorage serve the verify-cases fixtures. Effect functions are
// wrapped by the replay (`globalThis.__sankaRun`) so their intents can be attributed.
// No network, no TCP listener, no real react-native package.
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { AsyncLocalStorage } = require("node:async_hooks");

const { createStubs, IMAGE_EXTENSIONS } = require("./react-native-stub.js");

const INTERACTIVE = { button: "press", switch: "toggle", textfield: "type" };
const HANDLERS = { press: "onPress", toggle: "onValueChange", type: "onChangeText" };
const FLUSH_ROUNDS = 8;

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

// The fetch mock resolves requests by method and concrete url to a captured effect and
// serves that effect's fixture in the mode the scenario selects.
function installFetch(recorder) {
  globalThis.fetch = (input, init) => {
    const url = typeof input === "string" ? input : String(input && input.url);
    const options = init || {};
    const method = String(options.method || "GET").toUpperCase();
    const effect = (recorder.effects || []).find(
      (item) => item.method === method && item.url === url
    );
    if (!effect) throw new Error(`fetch ${method} ${url} matches no captured effect`);
    const headers = {};
    for (const [name, value] of Object.entries(options.headers || {})) headers[name] = String(value);
    const body = options.body === undefined ? null : JSON.parse(String(options.body));
    recorder.requests.push({ effect: effect.id, method, url, headers, body });
    const mode = recorder.modes[effect.id] || recorder.defaultMode;
    if (mode === "pending") return new Promise(() => {});
    const entry = recorder.fixtures[effect.id];
    if (!entry) throw new Error(`no fixture for effect ${effect.id}`);
    const fixture = entry[mode === "success" ? "response" : "failure"];
    if (!fixture) throw new Error(`no ${mode} fixture for effect ${effect.id}`);
    if (fixture.error !== undefined) {
      return Promise.reject(new TypeError("fixture network failure"));
    }
    const status = Number(fixture.status);
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(clone(fixture.body)),
    });
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

async function main() {
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
    effects: [],
    fixtures: {},
    storage: {},
    storagePending: false,
    defaultMode: "success",
    modes: {},
    context: new AsyncLocalStorage(),
    entries: [],
    requests: [],
    // Intents raised inside an effect body (even after its awaits) are attributed to
    // that effect through the async context; everything else is depth 0.
    record(intent) {
      const inside = this.context.getStore();
      this.entries.push({ intent: clone(intent), depth: inside ? 1 : 0 });
    },
    reset() {
      this.entries = [];
      this.requests = [];
    },
  };
  globalThis.__sankaRun = (effect, body) => {
    const inside = recorder.context.getStore();
    recorder.entries.push({ intent: { run: effect }, depth: inside ? 1 : 0, marker: true });
    return recorder.context.run({ effect }, () => body());
  };
  const stubs = createStubs(React, recorder);
  const wrappedReact = wrapReact(React, recorder);
  installLoader(sourceRoot, stubs.modules, wrappedReact);
  installFetch(recorder);
  globalThis.React = wrappedReact;
  const roles = spec.roles;
  const probeText = spec.probe_text;
  const fixtureStorage = (spec.fixtures && spec.fixtures.storage) || {};

  const flush = async () => {
    await act(async () => {
      for (let round = 0; round < FLUSH_ROUNDS; round += 1) {
        await new Promise((resolve) => setImmediate(resolve));
      }
    });
  };

  const documents = [];
  for (const screen of spec.screens) {
    const loaded = require(path.join(sourceRoot, screen.file));
    const Component = loaded && loaded.__esModule && loaded.default ? loaded.default : loaded;
    if (typeof Component !== "function") {
      throw new Error(`${screen.file} does not default-export a function component`);
    }
    const screenFixtures =
      (spec.fixtures && spec.fixtures.screens && spec.fixtures.screens[screen.module]) || {};
    const props =
      screen.navigation === "react-navigation"
        ? { navigation: stubs.navigation, route: { params: screen.params } }
        : {};
    function Harness() {
      recorder.cursor = 0;
      return Component(props);
    }
    const hasAppear = Array.isArray(screen.appear) && screen.appear.length > 0;
    const submitEffects = new Set(screen.submit_effects || []);

    // Mounts the screen with the given fixture modes and settles its appear effects.
    const mount = async (defaultMode, modes) => {
      recorder.stateNames = screen.state;
      recorder.params = screen.params;
      recorder.effects = screen.effects || [];
      recorder.fixtures = screenFixtures.effects || {};
      recorder.storage = { ...fixtureStorage };
      recorder.storagePending = defaultMode === "pending";
      recorder.defaultMode = defaultMode;
      recorder.modes = modes || {};
      recorder.reset();
      let renderer;
      await act(async () => {
        renderer = TestRenderer.create(React.createElement(Harness));
      });
      await flush();
      return renderer;
    };
    const unmount = async (renderer) => {
      await act(async () => renderer.unmount());
    };
    const activate = async (node, kind) => {
      const handler = node._props[HANDLERS[kind]];
      if (typeof handler !== "function") return null;
      let probe;
      if (kind === "toggle") probe = !node.checked;
      else if (kind === "type") probe = probeText;
      await act(async () => {
        if (kind === "press") handler();
        else handler(probe);
      });
      await flush();
      return probe;
    };
    const raised = () =>
      recorder.entries.filter((entry) => !entry.marker).map((entry) => entry.intent);
    // Replays a scenario's activation steps on a freshly mounted screen.
    const replaySteps = async (renderer, steps) => {
      for (const step of steps) {
        const tree = normalize(renderer.toJSON(), roles);
        await activate(locate(tree, step.path), step.kind);
      }
    };
    // A node's action: the intents its handler produced outside effects, with each effect
    // start recorded as a run intent (effect internals belong to `raised`).
    const probeAction = async (baseMode, modes, steps, indexes, kind) => {
      const renderer = await mount(baseMode, modes);
      try {
        await replaySteps(renderer, steps);
        const tree = normalize(renderer.toJSON(), roles);
        const target = locate(tree, indexes);
        if (typeof target._props[HANDLERS[kind]] !== "function") return null;
        recorder.reset();
        const probe = await activate(target, kind);
        return recorder.entries
          .filter((entry) => entry.depth === 0)
          .map((entry) => {
            const intent = entry.intent;
            const bound =
              kind !== "press" &&
              "set" in intent &&
              JSON.stringify(intent.value) === JSON.stringify(probe);
            return bound ? { bind: intent.set } : intent;
          });
      } finally {
        await unmount(renderer);
      }
    };
    const withActions = async (tree, baseMode, modes, steps) => {
      for (const { path: indexes, node } of interactive(tree, [], [])) {
        node.action = await probeAction(baseMode, modes, steps, indexes, INTERACTIVE[node.role]);
      }
      return tree;
    };
    const document = (scenario, tree, raisedIntents, requests) => ({
      screen: screen.name,
      scenario,
      params: screen.params,
      tree: strip(tree),
      raised: raisedIntents,
      requests,
    });
    // Appear-only scenarios: mount in a mode, read the tree, probe actions in that mode.
    const appearScenario = async (scenario, mode) => {
      const renderer = await mount(mode, {});
      let tree;
      let intents;
      let requests;
      try {
        tree = normalize(renderer.toJSON(), roles);
        intents = raised();
        requests = recorder.requests.slice();
      } finally {
        await unmount(renderer);
      }
      await withActions(tree, mode, {}, []);
      return document(scenario, tree, intents, requests);
    };

    const initial = await appearScenario("initial", "pending");
    documents.push(initial);
    let base = initial;
    let baseMode = "pending";
    if (hasAppear) {
      base = await appearScenario("loaded", "success");
      documents.push(base);
      documents.push(await appearScenario("load-failed", "failure"));
      baseMode = "success";
    }
    for (const entry of interactive(base.tree, [], [])) {
      const kind = INTERACTIVE[entry.node.role];
      const action = entry.node.action;
      if (!Array.isArray(action)) continue;
      const submits = action.filter((intent) => "run" in intent && submitEffects.has(intent.run));
      const applies = action.some((intent) => (kind === "press" ? "set" in intent : "bind" in intent));
      const variants = submits.length
        ? [
            ["submit", "success"],
            ["submit-failed", "failure"],
          ]
        : applies
          ? [[kind, "success"]]
          : [];
      for (const [prefix, mode] of variants) {
        const modes = {};
        for (const intent of submits) modes[intent.run] = mode;
        const step = { kind, path: entry.path };
        const renderer = await mount(baseMode, modes);
        let after;
        let intents;
        let requests;
        try {
          const tree = normalize(renderer.toJSON(), roles);
          recorder.reset();
          await activate(locate(tree, entry.path), kind);
          after = normalize(renderer.toJSON(), roles);
          intents = raised();
          requests = recorder.requests.slice();
        } finally {
          await unmount(renderer);
        }
        await withActions(after, baseMode, modes, [step]);
        documents.push(document(`${prefix}:${entry.path.join(".")}`, after, intents, requests));
      }
    }
  }
  fs.writeFileSync(destination, JSON.stringify(documents) + "\n");
}

main().catch((error) => {
  console.error(String((error && error.stack) || error));
  process.exit(1);
});
