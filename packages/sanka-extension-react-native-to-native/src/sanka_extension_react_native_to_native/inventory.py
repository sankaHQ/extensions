# SPDX-License-Identifier: Apache-2.0
"""Static inventory of what the app needs beyond the screen envelope.

Dependencies, native modules and permissions are classified from manifests without
importing or executing anything. Unknown packages are reported, never guessed away.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

RUNTIME = frozenset(
    {
        "react",
        "react-native",
        "expo",
        "expo-router",
        "expo-status-bar",
        "expo-constants",
        "expo-linking",
        "expo-splash-screen",
        "expo-system-ui",
        "expo-font",
        "react-native-screens",
        "react-native-safe-area-context",
        "react-native-web",
        "react-dom",
        "@react-navigation/native",
        "@react-navigation/native-stack",
        "@react-navigation/bottom-tabs",
        "@react-navigation/elements",
    }
)
TOOLING_PREFIXES = (
    "@babel/",
    "@types/",
    "@react-native/",
    "@react-native-community/cli",
    "@expo/",
    "eslint",
    "prettier",
    "jest",
    "typescript",
    "metro",
    "babel-",
    "react-test-renderer",
    "@testing-library/",
)
ANIMATION = frozenset(
    {"react-native-reanimated", "react-native-gesture-handler", "lottie-react-native", "moti"}
)
STATE = frozenset(
    {
        "redux",
        "@reduxjs/toolkit",
        "react-redux",
        "zustand",
        "mobx",
        "mobx-react",
        "mobx-react-lite",
        "@tanstack/react-query",
        "jotai",
        "recoil",
        "swr",
        "@apollo/client",
    }
)
UI_PREFIXES = (
    "react-native-paper",
    "native-base",
    "@rneui/",
    "react-native-elements",
    "tamagui",
    "@tamagui/",
    "@gluestack-ui/",
    "react-native-vector-icons",
    "@expo/vector-icons",
    "react-native-svg",
)
_PLIST_USAGE = re.compile(r"<key>(NS[A-Za-z]+UsageDescription)</key>")
_ANDROID_PERMISSION = re.compile(r'<uses-permission[^>]*android:name="([^"]+)"')


def classify(name: str) -> str:
    if name in RUNTIME:
        return "runtime"
    if name.startswith(TOOLING_PREFIXES):
        return "tooling"
    if name in ANIMATION:
        return "animation"
    if name in STATE:
        return "state"
    if name.startswith(UI_PREFIXES):
        return "ui"
    if name.startswith(("expo-", "react-native-", "@react-native-firebase/", "@react-native-")):
        return "native"
    return "other"


def inventory(root: Path, package: dict[str, Any]) -> dict[str, Any]:
    dependencies = []
    for section in ("dependencies", "devDependencies"):
        group = package.get(section)
        if not isinstance(group, dict):
            continue
        for name, version in sorted(group.items()):
            dependencies.append(
                {
                    "name": str(name),
                    "version": str(version),
                    "kind": classify(str(name)),
                    "dev": section == "devDependencies",
                }
            )
    permissions: set[str] = set()
    for plist in sorted(root.glob("ios/*/Info.plist")):
        if plist.is_symlink() or plist.stat().st_size > 512 * 1024:
            continue
        permissions.update(
            f"ios:{key}" for key in _PLIST_USAGE.findall(plist.read_text(errors="replace"))
        )
    manifest = root / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    if manifest.is_file() and not manifest.is_symlink() and manifest.stat().st_size <= 512 * 1024:
        permissions.update(
            f"android:{key}"
            for key in _ANDROID_PERMISSION.findall(manifest.read_text(errors="replace"))
        )
    expo = _expo_config(root)
    ios_plist = expo.get("ios", {}).get("infoPlist") if isinstance(expo.get("ios"), dict) else None
    if isinstance(ios_plist, dict):
        permissions.update(
            f"ios:{key}" for key in ios_plist if str(key).endswith("UsageDescription")
        )
    android = expo.get("android") if isinstance(expo.get("android"), dict) else {}
    declared = android.get("permissions") if isinstance(android, dict) else None
    if isinstance(declared, list):
        permissions.update(f"android:{item}" for item in declared if isinstance(item, str))
    return {
        "dependencies": dependencies,
        "native_modules": sorted(
            item["name"]
            for item in dependencies
            if item["kind"] in {"native", "animation", "ui", "state", "other"} and not item["dev"]
        ),
        "permissions": sorted(permissions),
    }


def _expo_config(root: Path) -> dict[str, Any]:
    path = root / "app.json"
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    expo = data.get("expo") if isinstance(data, dict) else None
    return expo if isinstance(expo, dict) else {}
