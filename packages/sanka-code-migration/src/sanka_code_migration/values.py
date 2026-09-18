# SPDX-License-Identifier: Apache-2.0
"""Scalar input presence for cross-language validation; no source coercion policy."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

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


@dataclass(frozen=True, slots=True)
class DecimalValue:
    """Exact coefficient/exponent representation; no rounding or float conversion.

    Scale and signed zero are retained. Source precision/scale validation and target
    database bounds remain separate policies; this type performs no arithmetic.
    """

    coefficient: str
    exponent: int

    def __post_init__(self) -> None:
        if type(self.coefficient) is not str or not re.fullmatch(
            r"-?(0|[1-9][0-9]*)", self.coefficient
        ):
            raise ValueError("coefficient must be canonical signed decimal digits")
        if type(self.exponent) is not int:
            raise TypeError("exponent must be an integer")
        # Check representability without expanding large powers of ten.
        self.to_decimal()

    @classmethod
    def from_decimal(cls, value: Decimal) -> DecimalValue:
        if type(value) is not Decimal or not value.is_finite():
            raise ValueError("a finite Decimal is required")
        parts = value.as_tuple()
        return cls(
            ("-" if parts.sign else "") + "".join(str(digit) for digit in parts.digits),
            int(parts.exponent),
        )

    def to_decimal(self) -> Decimal:
        digits = self.coefficient.removeprefix("-")
        return Decimal(
            (int(self.coefficient.startswith("-")), tuple(map(int, digits)), self.exponent)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "sanka.decimal-value/v1",
            "coefficient": self.coefficient,
            "exponent": self.exponent,
        }


@dataclass(frozen=True, slots=True)
class TimestampValue:
    """An exact instant and original fixed offset, not a timezone-rule policy.

    Named-zone identity, future DST arithmetic and naive datetimes are outside this
    contract. Fractional-second offsets cannot be represented by the Go adapter.
    """

    utc: str
    offset_seconds: int

    def __post_init__(self) -> None:
        if type(self.utc) is not str or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", self.utc
        ):
            raise ValueError("utc must be a canonical UTC timestamp with six fractional digits")
        if type(self.offset_seconds) is not int or not -86400 < self.offset_seconds < 86400:
            raise ValueError("offset_seconds must be an integer strictly within one day")
        # Validate the date and both UTC/local representability; never clip boundaries.
        self.to_datetime()

    @classmethod
    def from_datetime(cls, value: datetime) -> TimestampValue:
        if type(value) is not datetime or value.tzinfo is None:
            raise ValueError("an aware datetime is required")
        offset = value.utcoffset()
        if offset is None:
            raise ValueError("an aware datetime is required")
        if offset.microseconds:
            raise ValueError("fractional-second UTC offsets are unsupported")
        utc = value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return cls(utc, offset.days * 86400 + offset.seconds)

    def to_datetime(self) -> datetime:
        return datetime.fromisoformat(self.utc).astimezone(
            timezone(timedelta(seconds=self.offset_seconds))
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "sanka.timestamp-value/v1",
            "utc": self.utc,
            "offset_seconds": self.offset_seconds,
        }
