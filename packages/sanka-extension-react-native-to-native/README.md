# React Native to native (experimental)

Extension ID: `sanka/react-native-to-native`. Source: React Native (TypeScript)
with React Navigation or Expo Router. Targets: `swiftui`, `compose`.

This version implements **scan and plan only**. It captures the navigation graph,
each screen's element tree, literal styles, local state and event bindings, and
gives every screen a disposition (`native-screen` or `needs-manual-adaptation`
with reason codes). SwiftUI and Jetpack Compose generation, `test` and `verify`
arrive with the emitter milestones; `apply` is refused until then. It is **not a
complete app migration**, not published in the extension catalog, and not
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
  "min_sdk": "26"
}
```

`target` is accepted as an alias of `target_framework` (the CLI forwards `--to`).
`navigation` is detected from `package.json` (`expo-router` plus an `app/`
directory, otherwise `@react-navigation/native` plus `App.tsx`) unless set.
`entry` overrides the React Navigation entry module. Bundle identifiers default to
the Expo `app.json` values. `min_ios` and `min_sdk` accept a fixed list.

## What scan captures (slice 1)

Sources are parsed with the vendored TypeScript compiler through `sanka-ts-capture`;
nothing is imported or executed.

- Project: `package.json` (React Native 0.76 or later), `app.json`, the module
  graph reachable from the entry through relative imports, an inventory of every
  dependency (runtime, tooling, animation, state, ui, native, other) and of
  declared iOS/Android permissions.
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

## Artifacts

`scan` writes `scan.json`; `plan` writes `plan.json` with the capture, an empty
`files` map, `generated: false`, `readiness`, per-screen `dispositions` and a
`plan_hash`. Plans are byte-identical across checkout locations for the same
source and configuration.
