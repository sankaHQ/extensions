// SPDX-License-Identifier: Apache-2.0
// Minimal stand-ins for the captured React Native, Expo Router and React Navigation
// surface. Each component is a plain function component rendering a host element of the
// same name, so react-test-renderer's JSON tree exposes exactly the captured props.
// Navigation hooks record typed intents instead of navigating. Nothing here reproduces
// layout, gestures or native behaviour; the structural replay compares trees only.
"use strict";

const IMAGE_EXTENSIONS = /\.(png|jpe?g|gif|webp)$/i;

function createStubs(React, recorder) {
  const h = React.createElement;

  function host(name) {
    return function Component(props) {
      const { children, ...rest } = props || {};
      return h(name, rest, children);
    };
  }

  function FlatList(props) {
    const { data, renderItem, keyExtractor, ...rest } = props || {};
    const items = Array.isArray(data) ? data : [];
    const children = items.map((item, index) => {
      const key = keyExtractor ? String(keyExtractor(item, index)) : String(index);
      return h("ListItem", { key: `${index}:${key}`, itemKey: key }, renderItem({ item, index }));
    });
    return h("FlatList", rest, ...children);
  }

  function StatusBar() {
    return h("StatusBar", {});
  }

  // Concrete hrefs resolve to the matching route pattern plus segment parameters, the
  // same normalization the emitter applies, so both sides report one intent shape.
  function resolveHref(href) {
    let pathname;
    let params = {};
    if (typeof href === "string") {
      pathname = href;
    } else if (href && typeof href === "object" && typeof href.pathname === "string") {
      pathname = href.pathname;
      params = href.params && typeof href.params === "object" ? { ...href.params } : {};
    } else {
      throw new Error("unsupported href " + JSON.stringify(href));
    }
    const routes = recorder.routes || [];
    if (routes.includes(pathname)) return { push: pathname, params };
    const segments = pathname.replace(/^\/+|\/+$/g, "");
    const parts = segments ? segments.split("/") : [];
    for (const pattern of routes) {
      const trimmed = pattern.replace(/^\/+|\/+$/g, "");
      const expected = trimmed ? trimmed.split("/") : [];
      if (expected.length !== parts.length) continue;
      const values = {};
      let matched = true;
      for (let index = 0; index < expected.length; index += 1) {
        const match = /^\[([A-Za-z_][A-Za-z0-9_]*)\]$/.exec(expected[index]);
        if (match) values[match[1]] = parts[index];
        else if (expected[index] !== parts[index]) {
          matched = false;
          break;
        }
      }
      if (matched) return { push: pattern, params: { ...values, ...params } };
    }
    throw new Error("href " + pathname + " matches no captured route");
  }

  const router = {
    push(href) {
      recorder.record(resolveHref(href));
    },
    back() {
      recorder.record({ back: true });
    },
  };

  function Link(props) {
    const { children, href, ...rest } = props || {};
    const wrapped = React.Children.map(children, (child) =>
      typeof child === "string" || typeof child === "number" ? h("Text", {}, child) : child
    );
    return h("Link", { ...rest, onPress: () => router.push(href) }, wrapped);
  }

  const navigation = {
    navigate(name, params) {
      recorder.record({ navigate: name, params: params && typeof params === "object" ? params : {} });
    },
    goBack() {
      recorder.record({ back: true });
    },
  };

  const reactNative = {
    View: host("View"),
    SafeAreaView: host("SafeAreaView"),
    ScrollView: host("ScrollView"),
    Text: host("Text"),
    Image: host("Image"),
    Pressable: host("Pressable"),
    TouchableOpacity: host("TouchableOpacity"),
    TextInput: host("TextInput"),
    Switch: host("Switch"),
    ActivityIndicator: host("ActivityIndicator"),
    FlatList,
    StyleSheet: { create: (styles) => styles },
  };
  const expoRouter = {
    Link,
    useRouter: () => router,
    useLocalSearchParams: () => ({ ...recorder.params }),
    Stack: host("Stack"),
    Tabs: host("Tabs"),
  };
  const expoStatusBar = { StatusBar };
  const reactNavigation = {
    useNavigation: () => navigation,
    NavigationContainer: host("NavigationContainer"),
  };
  // In-memory AsyncStorage fed by the fixture storage map; reads stay pending in the
  // `initial` scenario, writes record `store` intents and update the map.
  const asyncStorage = {
    getItem(key) {
      if (typeof key !== "string") throw new Error("AsyncStorage keys must be strings");
      if (recorder.storagePending) return new Promise(() => {});
      const value = recorder.storage[key];
      return Promise.resolve(value === undefined ? null : value);
    },
    setItem(key, value) {
      if (typeof key !== "string") throw new Error("AsyncStorage keys must be strings");
      recorder.record({ store: key, value: String(value) });
      recorder.storage[key] = String(value);
      return Promise.resolve();
    },
  };
  asyncStorage.default = asyncStorage;
  for (const module of [reactNative, expoRouter, expoStatusBar, reactNavigation, asyncStorage]) {
    module.__esModule = true;
  }
  return {
    modules: {
      "react-native": reactNative,
      "expo-router": expoRouter,
      "expo-status-bar": expoStatusBar,
      "@react-navigation/native": reactNavigation,
      "@react-native-async-storage/async-storage": asyncStorage,
    },
    navigation,
    IMAGE_EXTENSIONS,
  };
}

module.exports = { createStubs, IMAGE_EXTENSIONS };
