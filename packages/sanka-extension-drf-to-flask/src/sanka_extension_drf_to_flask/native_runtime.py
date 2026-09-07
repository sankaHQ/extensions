# SPDX-License-Identifier: Apache-2.0
"""Copied into generated targets; no DRF or Sanka runtime imports."""

import re
from types import SimpleNamespace
from typing import Any, ClassVar


class _ValidationError(Exception):
    def __init__(self, detail: Any) -> None:
        self.detail = detail if isinstance(detail, (dict, list)) else [str(detail)]


class AuthenticationFailed(Exception):
    def __init__(self, detail: Any = "Incorrect authentication credentials.") -> None:
        self.detail = detail


_exceptions = SimpleNamespace(ValidationError=_ValidationError)


class _Input:
    fields: ClassVar[dict[str, Any]]
    messages: ClassVar[dict[str, str]]
    non_field_errors: ClassVar[str]

    def __init__(self, *, data: Any) -> None:
        self._validated = False
        self.initial_data = data
        self.validated_data: dict[str, Any] = {}
        self.errors: dict[str, Any] = {}

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        return attrs

    def is_valid(self, *, raise_exception: bool = False) -> bool:
        if self._validated:
            if self.errors and raise_exception:
                raise _ValidationError(self.errors)
            return not self.errors
        self._validated = True
        data = self.initial_data
        if not isinstance(data, dict):
            message = (
                "No data provided"
                if data is None
                else self.messages["invalid"].format(datatype=type(data).__name__)
            )
            self.errors = {self.non_field_errors: [message]}
        else:
            values: dict[str, Any] = {}
            for name, field in self.fields.items():
                messages = field["messages"]
                if name not in data:
                    if field["required"]:
                        self.errors[name] = [messages["required"]]
                    continue
                value = data[name]
                if value is None:
                    if field["allow_null"]:
                        values[name] = None
                    else:
                        self.errors[name] = [messages["null"]]
                    continue
                errors = []
                if field["kind"] == "CharField":
                    if value == "" or (field["trim_whitespace"] and str(value).strip() == ""):
                        if field["allow_blank"]:
                            values[name] = ""
                        else:
                            self.errors[name] = [messages["blank"]]
                        continue
                    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                        self.errors[name] = [messages["invalid"]]
                        continue
                    value = str(value).strip() if field["trim_whitespace"] else str(value)
                    for key, invalid in (
                        (
                            "max_length",
                            field["max_length"] is not None and len(value) > field["max_length"],
                        ),
                        (
                            "min_length",
                            field["min_length"] is not None and len(value) < field["min_length"],
                        ),
                    ):
                        if invalid:
                            errors.append(messages[key].format(**{key: field[key]}))
                    if "\x00" in value:
                        errors.append("Null characters are not allowed.")
                    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                        errors.append(
                            "Surrogate characters are not allowed: U+{code:X}.".format(
                                code=next(
                                    ord(char) for char in value if 0xD800 <= ord(char) <= 0xDFFF
                                )
                            )
                        )
                else:
                    if isinstance(value, str) and len(value) > 1000:
                        self.errors[name] = [messages["max_string_length"]]
                        continue
                    try:
                        value = int(re.sub(r"\.0*\s*$", "", str(value)))
                    except (ValueError, TypeError):
                        self.errors[name] = [messages["invalid"]]
                        continue
                    for key, invalid in (
                        (
                            "max_value",
                            field["max_value"] is not None and value > field["max_value"],
                        ),
                        (
                            "min_value",
                            field["min_value"] is not None and value < field["min_value"],
                        ),
                    ):
                        if invalid:
                            errors.append(messages[key].format(**{key: field[key]}))
                if errors:
                    self.errors[name] = errors
                else:
                    values[name] = value
            if not self.errors:
                try:
                    self.validated_data = self.validate(values)
                except _ValidationError as error:
                    self.errors = (
                        error.detail
                        if isinstance(error.detail, dict)
                        else {self.non_field_errors: error.detail}
                    )
        if self.errors and raise_exception:
            raise _ValidationError(self.errors)
        return not self.errors
