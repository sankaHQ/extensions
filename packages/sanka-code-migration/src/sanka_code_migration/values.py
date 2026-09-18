# SPDX-License-Identifier: Apache-2.0
"""Scalar input presence for cross-language validation; no source coercion policy."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .ir import _canonical_json


@dataclass(frozen=True, slots=True)
class InputValue:
    """None means absent; the JSON string "null" means explicitly supplied null.

    Integers remain JSON text so consumers need not pass them through float64.
    Decimal, float, time and structured values need separate qualified contracts.
    """

    value_json: str | None = None

    def __post_init__(self) -> None:
        if self.value_json is None:
            return
        if type(self.value_json) is not str:
            raise TypeError("value_json must be JSON text or absent")
        value = json.loads(self.value_json)
        if value is not None and type(value) not in {bool, int, str}:
            raise ValueError("only null, boolean, integer and string inputs are qualified")
        if _canonical_json(value) != self.value_json:
            raise ValueError("value_json must be canonical JSON")
        self.value_json.encode("utf-8")

    @classmethod
    def from_field(cls, payload: dict[str, object], name: str) -> InputValue:
        if type(payload) is not dict or any(type(key) is not str for key in payload):
            raise TypeError("payload must be an object with string keys")
        if type(name) is not str or not name:
            raise ValueError("field name must be a non-empty string")
        if name not in payload:
            return cls()
        value = payload[name]
        if value is not None and type(value) not in {bool, int, str}:
            raise ValueError("only null, boolean, integer and string inputs are qualified")
        return cls(_canonical_json(value))

    @property
    def present(self) -> bool:
        return self.value_json is not None

    @property
    def is_null(self) -> bool:
        return self.value_json == "null"

    def to_dict(self) -> dict[str, object]:
        # JSON text is intentional: language runtimes must not round large integers.
        return {"schema": "sanka.input-value/v1", "value_json": self.value_json}
