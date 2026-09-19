# React Native to native (experimental)

Extension ID: `sanka/react-native-to-native`. Source: React Native (TypeScript)
with React Navigation or Expo Router. Targets: `swiftui` (scan, plan, apply, test,
verify) and `compose` (scan and plan only until the Compose emitter lands).

## Implementation notes

- 2026-09-19 (R2, SwiftUI emitter and tree replay): `plan` renders the SwiftUI package
  for `target_framework: swiftui`; `apply` writes it review-bound; `test` builds it and
  dumps the normalized screen trees; `verify` compares them with the React Native source.
  Deviations from the plan text, decided for this milestone:
  - No ViewInspector and no XCTest. The target tree comes from generated
    `tree(state:params:)` functions and the `sanka-tree-dump` executable run with
    `swift run`, because the build host has no licensed Xcode (`swift test` cannot load
    XCTest there). The SwiftUI `body` and the tree function are emitted from the same IR
    by the same emitter walk.
  - The source harness renders against an in-repo React Native stub
    (`node/react-native-stub.js`), not the real `react-native` package, because real RN
    rendering needs the Metro/Babel/Jest preset. The stub implements exactly the captured
    component subset as plain function components.
  - The iOS simulator destination is not built locally; CI on macOS with a licensed
    Xcode may add it later. The iOS shell is an XcodeGen spec (`App/project.yml`), never a
    generated `pbxproj`.
- 2026-09-17 (R0 + R1): scan and plan with per-screen dispositions.

It is **not a complete app migration**, not published in the extension catalog, and not
qualified for production use.

Use the extension JSON subprocess protocol via
`sanka-extension-react-native-to-native`, with configuration:

```json
{
  "target_framework": "swiftui",
  "navigation": "",
  "entry": "",
  "bundle_id": "",
  "package_name": "",
  "min_ios": "17.0",
  "min_sdk": "26",
  "extension_plan_hash": "sha256:..."
}
```

`target` is accepted as an alias of `target_framework` (the CLI forwards `--to`).
`navigation` is detected from `package.json` (`expo-router` plus an `app/`
directory, otherwise `@react-navigation/native` plus `App.tsx`) unless set.
`entry` overrides the React Navigation entry module. Bundle identifiers default to
the Expo `app.json` values (or `com.example.<name>` in the XcodeGen spec). `min_ios`
and `min_sdk` accept a fixed list. `extension_plan_hash` is required by `apply` and must
equal the reviewed plan's `plan_hash`.

## What scan captures (slice 1)

Sources are parsed with the vendored TypeScript compiler through `sanka-ts-capture`;
nothing is imported or executed.

- Project: `package.json` (React Native 0.76 or later), `app.json`, the module
  graph reachable from the entry through relative imports, an inventory of every
  dependency (runtime, tooling, animation, state, ui, native, other) and of
  declared iOS/Android permissions, and the digest of every referenced image asset.
- React Navigation: `createNativeStackNavigator` / `createBottomTabNavigator`
  with a literal object-type parameter list, one `NavigationContainer`, literal
  `<X.Screen name component options={{ title, headerShown }} />` entries, and
  navigators nested through local components up to two levels.
- Expo Router: routes from the `app/` tree (`index`, static files, one dynamic
  `[id]` segment per route, route groups `(name)`), `_layout.tsx` files returning
  `<Stack>` or `<Tabs>` with literal `<X.Screen>` options, `+` files ignored.
- Screens: a default-exported function component taking `{ navigation, route }`
  (React Navigation) or nothing (Expo Router), `useState` with literal initial
  values, `useRouter`, `useNavigation`, `useLocalSearchParams<{ id: string }>()`,
  and one JSX return.
- Elements: `View`, `SafeAreaView`, `ScrollView`, `Text`, `Image` (`require`d
  asset or `{ uri }`), `Pressable`, `TouchableOpacity`, `TextInput`, `Switch`,
  `FlatList` (`data`, `renderItem` with `{ item }`, `keyExtractor`),
  `ActivityIndicator`, `StatusBar` from `expo-status-bar`, `Link` from `expo-router`,
  fragments, conditionals (`cond ? <A/> : null`, `cond && <A/>`), `array.map`
  over state, and text runs built from literals, template strings, state, route
  parameters and list items.
- Styles: `StyleSheet.create` literals with flex layout, spacing, sizes, colors
  (hex, rgb(a), a few names), borders, typography and opacity.
- Events: `onPress`, `onChangeText`, `onValueChange` bound to a state setter,
  `navigation.navigate("Name", { literal params })`, `navigation.goBack()`,
  `router.push("/path")` / `router.push({ pathname, params })`, `router.back()`,
  with one to three actions per handler.

Everything else is a reason code on the screen, for example
`SANKA_RN_NATIVE_MODULE`, `SANKA_RN_THIRD_PARTY_COMPONENT`,
`SANKA_RN_CUSTOM_COMPONENT`, `SANKA_RN_UNSUPPORTED_HOOK`, `SANKA_RN_UNSUPPORTED_PROP`,
`SANKA_RN_DYNAMIC_STYLE`, `SANKA_RN_EXPRESSION`, `SANKA_RN_SYMBOL`, `SANKA_RN_EVENT`,
`SANKA_RN_PARAM`, `SANKA_RN_EXPO_LAYOUT`, `SANKA_RN_EXPO_DYNAMIC_ROUTE` and
`SANKA_RN_NAVIGATION`. Additional modules that the entry does not reach, catch-all
routes, syntax errors and unsupported React Native versions are project gaps.
Readiness is native screens divided by screens. Data loading (`fetch`, effects),
forms beyond local state, `AsyncStorage`, custom components, animations, state
libraries and platform branches are later slices.

## SwiftUI generation

`plan` renders every file in memory (`files`, text) plus an `assets` map (bundle path
to source path and SHA-256) and hashes the whole plan. The generated layout:

```text
Package.swift                      swift-tools-version 5.10, platforms iOS 17 (from min_ios) + macOS 14, no dependencies
Sources/AppUI/Navigation.swift     enum Route: Hashable (typed params), AppRoot (NavigationStack, TabView when tabs exist), SankaNavigator environment
Sources/AppUI/Screens/<Name>.swift <Name>State, <Name>Params, enum <Name>Action, apply(_:to:), body, tree(state:params:)
Sources/AppUI/Styles.swift         SankaStyle values from StyleSheet entries and the bounded modifier applying them
Sources/AppUI/Tree.swift           Node, Intent, JSONValue, the SankaScreen protocol and the replay driver
Sources/AppUI/Models.swift         value types inferred from literal state (only when any exist)
Sources/AppUI/Resources/<path>     image assets copied byte for byte
Sources/SankaTreeDump/main.swift   executable printing every native screen's scenarios as JSON
App/<AppName>App.swift, App/project.yml   iOS shell as an XcodeGen 2.46.0 spec (no pbxproj)
contract.json, README.md           the canonical capture and the generated package's own notes
```

Design: each screen is state + reducer + view. `<Name>State` holds the `useState`
values with their literal initial values; `enum <Name>Action` has one case per distinct
captured event action (`setTitle(String)`, `navigate(Route)`, `back`); the reducer
`apply(_:to:)` is the only place state changes; handlers are static functions returning
typed actions that the `body` dispatches and that `tree(state:params:)` reports as
intents. Navigation is surfaced through the `sankaNavigator` environment value
(`navigate(Route)` / `back()`), which `AppRoot` binds to its `NavigationStack` path.
Types: JavaScript numbers are `Double`, strings `String`, booleans `Bool`, arrays of
object literals become `<Screen><State>Item` value types with JSON encoding and
decoding. Both navigation profiles map to the same Swift shape (`Route` cases from
screen names or route patterns; Expo `push` targets resolve to route patterns).

Identifier rules: screen component `Name` keeps its name when it ends with `Screen`,
otherwise becomes `NameScreen`; route cases are lowerCamel from the screen name or the
static route segments. Collisions, non-identifiers and Swift reserved words are gaps
(`SANKA_RN_SWIFTUI_IDENTIFIER`), never silent renames. Screens the emitter cannot
express are dispositions with `SANKA_RN_SWIFTUI_TYPE`, `SANKA_RN_SWIFTUI_TREE`,
`SANKA_RN_SWIFTUI_EXPRESSION`, `SANKA_RN_SWIFTUI_NAVIGATION` or
`SANKA_RN_SWIFTUI_ASSET`, and become placeholder views that compile and show the
reason; `verify` fails while any remains.

`apply` requires `reviewed_plan_hash` and a matching `extension_plan_hash`, recomputes
the plan from the current source, refuses drift or an existing output, and writes
`.sanka/<artifact>/native-swiftui/` through a staging directory (assets are copied from
the project after checking their digests).

## Structural parity replay

`test` copies the applied output to a temporary directory (refusing when
`Package.swift`, `contract.json`, `App/project.yml` or any asset differs from the plan),
runs `swift build` and `swift run sanka-tree-dump` for the macOS destination, and writes
`test.json` with `swift_version`, `platform`, the observed scenario trees per screen
and `candidate_digest`. Build products live in `.sanka/<artifact>/native-swiftui-build`.

`verify` additionally transpiles the native source screens with the vendored
TypeScript compiler and renders them in Node 22 with `react` and
`react-test-renderer` (pinned in `tests/node-tools/package.json`) against
`node/react-native-stub.js`: plain function components for the captured component
subset, `StyleSheet.create` identity, `Link`/`useRouter`/`useLocalSearchParams`/
`useNavigation` stubs that record typed intents, and a `require()` hook that turns image
imports into `{ asset: path }` tokens. Wrapped `useState` setters record `set` intents
by declaration order. The harness applies the same scenarios, normalizes the rendered
tree into the shared node shape and writes JSON; `verify.json` (schema
`sanka.react-native-to-native.replay/v1`) carries `ok`, `candidate`, `source`,
`failures`, `swift_version`, `node_version`, `react_version`, `scope` and
`complete_app: false`. Comparison is canonical JSON equality per screen and scenario.
The source is re-captured before and after replay and the candidate snapshot is
compared; drift discards the observations. No TCP listener is opened anywhere.

### Normalized tree vocabulary

Defined once in `tree.py` (the emitter reads it, the Node harness receives it):

```text
node    {"role", "text", "label", "enabled", "checked", "action", "children"}
role    container | text | button | image | textfield | switch | list | listitem | indicator
        View/SafeAreaView/ScrollView/StatusBar (and a root fragment) -> container
        Text -> text (text = rendered run; JSX children follow React rules, templates JS coercion)
        Pressable/TouchableOpacity/Link -> button (enabled = !disabled, action from onPress)
        Image -> image (text = asset path or uri)
        TextInput -> textfield (text = bound value, label falls back to placeholder)
        Switch -> switch (checked = bound value)
        FlatList -> list of listitem (text = keyExtractor result or index)
        ActivityIndicator -> indicator
label   accessibilityLabel on every role; unused fields are null; action is null without a handler
intent  {"navigate": name, "params": {...}} | {"push": pattern, "params": {...}} | {"back": true}
        | {"set": state, "value": ...} | {"bind": state}   (values evaluated in the scenario state)
scenario initial | press:<path> (buttons with a set intent) | toggle:<path> (switches with bind)
        | type:<path> (text fields with bind, typed text "Sanka"); <path> = child-index path
params  deterministic samples per type: "sample-<name>", 1, true
```

### Environment

- `SANKA_NODE`: Node.js executable (major 22 required by `verify`; scan and plan accept
  20 or later). `SANKA_NODE_TOOLS`: directory containing `react` and
  `react-test-renderer` (defaults to the project's `node_modules`).
- `DEVELOPER_DIR`: forwarded to `swift`; set it to `/Library/Developer/CommandLineTools`
  on a Mac whose Xcode license is not accepted.
- Tests: `SANKA_SWIFT_TESTS=1` enables the `swift build` replay tests (macOS),
  `SANKA_NODE_TESTS=1` installs the pinned renderer with `npm ci` for the source side.

### Not compared or not exercised

- Layout, colors, fonts, spacing and pixels: Flexbox is approximated with stacks,
  spacers and frames and never compared.
- The iOS simulator build and the XcodeGen project (`App/project.yml` needs `xcodegen
  2.46.0` and Xcode 16 or later); locally the macOS destination is built instead.
- `keyboardType`, `autoCapitalize`, `ActivityIndicator` size/color, `resizeMode`
  values beyond fit/fill, and `numberOfLines` beyond `lineLimit`.
- Non-integral number formatting follows Swift's shortest representation; numbers beyond
  2^53 are not qualified.

## Artifacts

`scan` writes `scan.json`; `plan` writes `plan.json` with the capture, `files`,
`assets`, `generated`, `gaps`, `readiness`, per-screen `dispositions` (capture and
emitter reasons merged) and a `plan_hash`. Plans are byte-identical across checkout
locations and `PYTHONHASHSEED` values for the same source and configuration. `apply`
writes `native-swiftui/`; `test` and `verify` write `test.json` and `verify.json`.
