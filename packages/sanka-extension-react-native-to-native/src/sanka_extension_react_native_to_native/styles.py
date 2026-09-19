# SPDX-License-Identifier: Apache-2.0
"""StyleSheet.create literal capture with a bounded property set."""

from __future__ import annotations

import json
import re
from typing import Any

from sanka_ts_capture import tree as t

from .expressions import Unsupported

NUMBER_PROPS = frozenset(
    {
        "flex",
        "gap",
        "rowGap",
        "columnGap",
        "padding",
        "paddingHorizontal",
        "paddingVertical",
        "paddingTop",
        "paddingBottom",
        "paddingLeft",
        "paddingRight",
        "margin",
        "marginHorizontal",
        "marginVertical",
        "marginTop",
        "marginBottom",
        "marginLeft",
        "marginRight",
        "width",
        "height",
        "minHeight",
        "minWidth",
        "borderRadius",
        "borderWidth",
        "borderTopWidth",
        "borderBottomWidth",
        "borderLeftWidth",
        "borderRightWidth",
        "fontSize",
        "lineHeight",
        "opacity",
    }
)
ENUM_PROPS: dict[str, frozenset[str]] = {
    "flexDirection": frozenset({"row", "column", "row-reverse", "column-reverse"}),
    "justifyContent": frozenset(
        {"flex-start", "flex-end", "center", "space-between", "space-around", "space-evenly"}
    ),
    "alignItems": frozenset({"flex-start", "flex-end", "center", "stretch", "baseline"}),
    "alignSelf": frozenset({"auto", "flex-start", "flex-end", "center", "stretch"}),
    "flexWrap": frozenset({"wrap", "nowrap"}),
    "textAlign": frozenset({"auto", "left", "right", "center", "justify"}),
    "fontWeight": frozenset(
        {"normal", "bold", "100", "200", "300", "400", "500", "600", "700", "800", "900"}
    ),
    "fontStyle": frozenset({"normal", "italic"}),
    "textTransform": frozenset({"none", "uppercase", "lowercase", "capitalize"}),
    "overflow": frozenset({"visible", "hidden"}),
}
COLOR_PROPS = frozenset(
    {
        "backgroundColor",
        "color",
        "borderColor",
        "borderTopColor",
        "borderBottomColor",
        "tintColor",
    }
)
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\Z")
_RGBA = re.compile(
    r"rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(?:,\s*(?:0|1|0?\.\d+)\s*)?\)\Z"
)
NAMED_COLORS = frozenset({"black", "white", "transparent", "red", "green", "blue", "gray", "grey"})
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def capture_styles(node: t.Node) -> dict[str, dict[str, Any]]:
    """Return ``{name: {prop: value}}`` for ``StyleSheet.create({...})``."""
    parts = t.call_parts(node)
    if parts is None or t.property_chain(parts[0]) != ("StyleSheet", "create"):
        raise Unsupported("SANKA_RN_DYNAMIC_STYLE", "styles must come from StyleSheet.create")
    if len(parts[1]) != 1:
        raise Unsupported("SANKA_RN_DYNAMIC_STYLE", "StyleSheet.create takes one object literal")
    literal = t.unparenthesize(parts[1][0])
    encoded = literal.get("json")
    if t.kind(literal) != "ObjectLiteralExpression" or not isinstance(encoded, str):
        raise Unsupported("SANKA_RN_DYNAMIC_STYLE", "StyleSheet.create needs a literal object")
    styles = json.loads(encoded)
    if not isinstance(styles, dict):
        raise Unsupported("SANKA_RN_DYNAMIC_STYLE", "StyleSheet.create needs a literal object")
    result: dict[str, dict[str, Any]] = {}
    for name, value in styles.items():
        if not IDENTIFIER.fullmatch(name) or not isinstance(value, dict):
            raise Unsupported("SANKA_RN_DYNAMIC_STYLE", f"style '{name}' must be a literal object")
        result[name] = {prop: _value(name, prop, item) for prop, item in value.items()}
    return result


def _value(style: str, prop: str, value: Any) -> Any:
    where = f"style '{style}.{prop}'"
    if prop in NUMBER_PROPS:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise Unsupported("SANKA_RN_DYNAMIC_STYLE", f"{where} must be a number")
        if prop == "opacity" and not 0 <= value <= 1:
            raise Unsupported("SANKA_RN_DYNAMIC_STYLE", f"{where} must be between 0 and 1")
        return value
    if prop in ENUM_PROPS:
        if not isinstance(value, str) or value not in ENUM_PROPS[prop]:
            raise Unsupported("SANKA_RN_DYNAMIC_STYLE", f"{where} has an unsupported value")
        return value
    if prop in COLOR_PROPS:
        if not isinstance(value, str) or not (
            _HEX.fullmatch(value) or _RGBA.fullmatch(value) or value in NAMED_COLORS
        ):
            raise Unsupported(
                "SANKA_RN_DYNAMIC_STYLE", f"{where} must be a hex, rgb(a) or named color"
            )
        return value
    raise Unsupported("SANKA_RN_DYNAMIC_STYLE", f"{where} is not a qualified style property")
