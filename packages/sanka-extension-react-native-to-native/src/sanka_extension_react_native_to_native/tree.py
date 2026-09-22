# SPDX-License-Identifier: Apache-2.0
"""Normalized screen-tree vocabulary shared by the SwiftUI emitter and the Node harness.

Both sides of the structural parity replay emit the same node shape for every screen
and scenario. Layout, colors and pixels are not part of it. Keep this module free of
rendering logic: the emitter reads it to generate Swift, ``replay`` passes it to the
Node harness, and the package README documents it.

Node shape (exactly these keys, in any order)::

    {"role": <role>, "text": str | null, "label": str | null, "enabled": bool | null,
     "checked": bool | null, "action": [intent, ...] | null, "children": [node, ...]}

Roles and what fills the fields:

- ``container``: ``View``, ``SafeAreaView``, ``ScrollView``, ``StatusBar`` and a
  fragment at the root of a screen. Fragments elsewhere and ``array.map`` results are
  spliced into the parent's children.
- ``text``: ``Text``; ``text`` is the rendered run after interpolation. JSX children
  follow React rendering rules (numbers are formatted like JavaScript, booleans and
  ``null`` render nothing); template literals follow JavaScript string coercion.
- ``button``: ``Pressable``, ``TouchableOpacity`` and Expo Router ``Link``;
  ``enabled`` is the negated ``disabled`` prop (``true`` when absent); ``action`` lists
  the intents of ``onPress``.
- ``image``: ``Image``; ``text`` is the project-relative asset path of a
  ``require()`` source or the literal URI of a ``{ uri }`` source.
- ``textfield``: ``TextInput``; ``text`` is the bound value, ``label`` falls back to
  ``placeholder``; ``action`` lists the intents of ``onChangeText``.
- ``switch``: ``Switch``; ``checked`` is the bound value; ``action`` lists the intents
  of ``onValueChange``.
- ``list``: ``FlatList``; children are ``listitem`` nodes whose ``text`` is the
  ``keyExtractor`` result (the item index when there is no extractor) and whose single
  child is the rendered item.
- ``indicator``: ``ActivityIndicator``.

``label`` is the ``accessibilityLabel`` prop on every role. Fields that do not apply
to a role are ``null``; ``action`` is ``null`` on nodes without a handler.

Intents (typed, values evaluated in the scenario state):

- ``{"navigate": <screen name>, "params": {...}}`` (React Navigation)
- ``{"push": <route pattern>, "params": {...}}`` (Expo Router; concrete hrefs are
  resolved to the matching route pattern plus segment parameters)
- ``{"back": true}``
- ``{"set": <state name>, "value": <new value>}``
- ``{"bind": <state name>}`` for a handler that assigns the event argument directly
- ``{"run": <effect id>}`` for a handler that starts a fetch effect
- ``{"store": <key>, "value": <string>}`` for an ``AsyncStorage.setItem`` call

Scenarios: ``initial`` (mounted, appear effects started, their requests still in
flight); ``loaded`` and ``load-failed`` when the screen runs effects on appear (every
fetch succeeds or fails with its fixture, storage reads resolve); then, from the
loaded state when it exists and the initial state otherwise, ``press:<path>`` for
each button whose action contains a ``set`` intent, ``toggle:<path>`` for each switch
and ``type:<path>`` for each text field whose action contains a ``bind`` intent (the
switch receives the negated value, the text field receives ``PROBE_TEXT``), and
``submit:<path>`` plus ``submit-failed:<path>`` for each button whose action runs a
POST effect. ``<path>`` is the child-index path of the node in that base tree
(``"2"``, ``"3.0.1"``; the root is ``""``).

Every scenario document also carries ``raised``, the intents produced while the
scenario ran (state assignments in order, navigation, storage writes), and
``requests``, the network requests made (``effect``, ``method``, ``url``,
``headers``, JSON ``body`` or null).
"""

from __future__ import annotations

from typing import Any

SCHEMA = "sanka.react-native-to-native.tree/v1"
ROLES = (
    "container",
    "text",
    "button",
    "image",
    "textfield",
    "switch",
    "list",
    "listitem",
    "indicator",
)
NODE_KEYS = ("role", "text", "label", "enabled", "checked", "action", "children")
COMPONENT_ROLES: dict[str, str] = {
    "View": "container",
    "SafeAreaView": "container",
    "ScrollView": "container",
    "StatusBar": "container",
    "Fragment": "container",
    "Text": "text",
    "Image": "image",
    "Pressable": "button",
    "TouchableOpacity": "button",
    "Link": "button",
    "TextInput": "textfield",
    "Switch": "switch",
    "FlatList": "list",
    "ActivityIndicator": "indicator",
}
INTENT_KINDS = ("navigate", "push", "back", "set", "bind", "run", "store")
DOCUMENT_KEYS = ("screen", "scenario", "params", "tree", "raised", "requests")
REQUEST_KEYS = ("effect", "method", "url", "headers", "body")
SCENARIO_KINDS: dict[str, str] = {"button": "press", "switch": "toggle", "textfield": "type"}
INTERACTIVE_ROLES = tuple(SCENARIO_KINDS)
PROBE_TEXT = "Sanka"
SAMPLE_NUMBER = 1
SAMPLE_BOOLEAN = True


def sample_params(params: dict[str, str]) -> dict[str, Any]:
    """Deterministic route parameter values used by both replay sides."""
    values: dict[str, Any] = {}
    for name in sorted(params):
        kind = params[name].rstrip("?")
        if kind == "string":
            values[name] = f"sample-{name}"
        elif kind == "number":
            values[name] = SAMPLE_NUMBER
        elif kind == "boolean":
            values[name] = SAMPLE_BOOLEAN
        else:
            raise ValueError(f"route parameter {name!r} has an unsupported type {kind!r}")
    return values


def validate_node(node: Any, where: str = "tree") -> None:
    """Raise ValueError unless ``node`` follows the shared node shape."""
    if not isinstance(node, dict) or set(node) != set(NODE_KEYS):
        raise ValueError(f"{where}: node must have exactly the keys {', '.join(NODE_KEYS)}")
    if node["role"] not in ROLES:
        raise ValueError(f"{where}: unknown role {node['role']!r}")
    for key in ("text", "label"):
        if node[key] is not None and type(node[key]) is not str:
            raise ValueError(f"{where}: {key} must be a string or null")
    for key in ("enabled", "checked"):
        if node[key] is not None and type(node[key]) is not bool:
            raise ValueError(f"{where}: {key} must be a boolean or null")
    action = node["action"]
    if action is not None:
        if not isinstance(action, list):
            raise ValueError(f"{where}: action must be a list of intents or null")
        for index, intent in enumerate(action):
            validate_intent(intent, f"{where}.action[{index}]")
    if not isinstance(node["children"], list):
        raise ValueError(f"{where}: children must be a list")
    for index, child in enumerate(node["children"]):
        validate_node(child, f"{where}.{index}")


def validate_intent(intent: Any, where: str) -> None:
    if not isinstance(intent, dict):
        raise ValueError(f"{where}: intents must be objects")
    kinds = [kind for kind in INTENT_KINDS if kind in intent]
    if len(kinds) != 1:
        raise ValueError(f"{where}: an intent carries exactly one of {', '.join(INTENT_KINDS)}")
    kind = kinds[0]
    expected = {
        "navigate": {"navigate", "params"},
        "push": {"push", "params"},
        "back": {"back"},
        "set": {"set", "value"},
        "bind": {"bind"},
        "run": {"run"},
        "store": {"store", "value"},
    }[kind]
    if set(intent) != expected:
        raise ValueError(f"{where}: {kind} intents carry exactly {', '.join(sorted(expected))}")
    if kind == "back":
        if intent["back"] is not True:
            raise ValueError(f"{where}: back intents carry true")
    elif type(intent[kind]) is not str:
        raise ValueError(f"{where}: {kind} target must be a string")
    if "params" in intent and not isinstance(intent["params"], dict):
        raise ValueError(f"{where}: params must be an object")
    if kind == "store" and type(intent["value"]) is not str:
        raise ValueError(f"{where}: stored values must be strings")


def validate_request(request: Any, where: str) -> None:
    if not isinstance(request, dict) or set(request) != set(REQUEST_KEYS):
        raise ValueError(f"{where}: requests carry exactly {', '.join(REQUEST_KEYS)}")
    for key in ("effect", "method", "url"):
        if type(request[key]) is not str:
            raise ValueError(f"{where}: {key} must be a string")
    if not isinstance(request["headers"], dict) or not all(
        type(value) is str for value in request["headers"].values()
    ):
        raise ValueError(f"{where}: headers must map names to strings")


def index_documents(documents: Any, screens: list[str]) -> dict[str, dict[str, Any]]:
    """Validate replay documents and index them as ``{screen: {scenario: document}}``."""
    if not isinstance(documents, list):
        raise ValueError("replay output must be a list of scenario documents")
    indexed: dict[str, dict[str, Any]] = {name: {} for name in screens}
    for position, document in enumerate(documents):
        where = f"document[{position}]"
        if not isinstance(document, dict) or set(document) != set(DOCUMENT_KEYS):
            raise ValueError(f"{where}: documents carry exactly {', '.join(DOCUMENT_KEYS)}")
        screen, scenario = document["screen"], document["scenario"]
        if screen not in indexed:
            raise ValueError(f"{where}: unexpected screen {screen!r}")
        if type(scenario) is not str or scenario in indexed[screen]:
            raise ValueError(f"{where}: duplicate or invalid scenario for {screen}")
        if not isinstance(document["params"], dict):
            raise ValueError(f"{where}: params must be an object")
        validate_node(document["tree"], f"{where}.tree")
        if not isinstance(document["raised"], list) or not isinstance(document["requests"], list):
            raise ValueError(f"{where}: raised and requests must be lists")
        for index, intent in enumerate(document["raised"]):
            validate_intent(intent, f"{where}.raised[{index}]")
        for index, request in enumerate(document["requests"]):
            validate_request(request, f"{where}.requests[{index}]")
        indexed[screen][scenario] = {
            key: document[key] for key in ("params", "tree", "raised", "requests")
        }
    for name, scenarios in indexed.items():
        if "initial" not in scenarios:
            raise ValueError(f"screen {name} has no initial scenario")
    return indexed
