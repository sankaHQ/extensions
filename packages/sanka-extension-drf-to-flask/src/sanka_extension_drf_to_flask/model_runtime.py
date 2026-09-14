# SPDX-License-Identifier: Apache-2.0
"""Native model serializer support copied into generated Flask projects."""

from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from .native_runtime import _Input, _ValidationError


class ModelInput(_Input):
    """Captured scalar fields and explicitly lowered nested write methods."""

    model: ClassVar[Any]

    def __init__(self, instance: Any = None, *, data: Any = None, partial: bool = False) -> None:
        super().__init__(data=data)
        self.instance = instance
        self.partial = partial

    def is_valid(self, *, raise_exception: bool = False) -> bool:
        if self._validated:
            if self.errors and raise_exception:
                raise _ValidationError(self.errors)
            return not self.errors
        self._validated = True
        if not isinstance(self.initial_data, dict):
            self.errors = {
                self.non_field_errors: [
                    "No data provided"
                    if self.initial_data is None
                    else self.messages["invalid"].format(datatype=type(self.initial_data).__name__)
                ]
            }
        else:
            for name, field in self.fields.items():
                if field["read_only"]:
                    continue
                if name not in self.initial_data:
                    if field["required"] and not self.partial:
                        self.errors[name] = [field["messages"]["required"]]
                    continue
                value = self.initial_data[name]
                try:
                    value = self.clean_field(field, value)
                    if field.get("unique") and value is not None:
                        query = self.model._default_manager.filter(**{name: value})
                        if self.instance is not None:
                            query = query.exclude(pk=self.instance.pk)
                        if query.exists():
                            raise _ValidationError([field["unique"]])
                    self.validated_data[name] = value
                except _ValidationError as error:
                    self.errors[name] = error.detail
        if self.errors:
            self.validated_data = {}
            if raise_exception:
                raise _ValidationError(self.errors)
        return not self.errors

    def clean_field(self, field: dict[str, Any], value: Any) -> Any:
        messages = field["messages"]
        if value is None:
            if field["allow_null"]:
                return None
            raise _ValidationError([messages["null"]])
        kind = field["kind"]
        if kind == "nested":
            if not isinstance(value, list):
                raise _ValidationError(
                    {
                        self.non_field_errors: [
                            messages["not_a_list"].format(input_type=type(value).__name__)
                        ]
                    }
                )
            if not value and not field["allow_empty"]:
                raise _ValidationError({self.non_field_errors: [messages["empty"]]})
            values, errors = [], []
            for item in value:
                child = field["serializer"](data=item, partial=self.partial)
                child.is_valid()
                values.append(child.validated_data)
                errors.append(child.errors)
            if any(errors):
                raise _ValidationError(
                    {i: error for i, error in enumerate(errors) if error}
                    if field["indexed_errors"]
                    else errors
                )
            return values
        if kind in {"CharField", "IntegerField"}:
            validator: Any = _Input(data={"value": value})
            validator.fields = {"value": field}
            validator.non_field_errors = self.non_field_errors
            validator.messages = self.messages
            if not validator.is_valid():
                raise _ValidationError(validator.errors["value"])
            return validator.validated_data["value"]
        if kind == "ChoiceField":
            if value == "" and field["allow_blank"]:
                return ""
            choices = {str(k): k for k in field["choices"]}
            if str(value) not in choices:
                raise _ValidationError([messages["invalid_choice"].format(input=value)])
            return choices[str(value)]
        if kind == "DecimalField":
            text = str(value).strip()
            if len(text) > 1000:
                raise _ValidationError([messages["max_string_length"]])
            try:
                number = Decimal(text)
            except InvalidOperation:
                raise _ValidationError([messages["invalid"]]) from None
            if not number.is_finite():
                raise _ValidationError([messages["invalid"]])
            _, digits, exponent = number.as_tuple()
            assert isinstance(exponent, int)
            total = len(digits) + exponent if exponent >= 0 else max(len(digits), -exponent)
            places = max(-exponent, 0)
            whole = total - places
            for key, actual in (
                ("max_digits", total),
                ("decimal_places", places),
                ("max_whole_digits", whole),
            ):
                maximum = field[key]
                if maximum is not None and actual > maximum:
                    message_key = "max_decimal_places" if key == "decimal_places" else key
                    raise _ValidationError([messages[message_key].format(**{message_key: maximum})])
            for key, bad in (
                (
                    "max_value",
                    field["max_value"] is not None and number > Decimal(str(field["max_value"])),
                ),
                (
                    "min_value",
                    field["min_value"] is not None and number < Decimal(str(field["min_value"])),
                ),
            ):
                if bad:
                    raise _ValidationError([messages[key].format(**{key: field[key]})])
            return number.quantize(Decimal(1).scaleb(-field["decimal_places"]))
        raise RuntimeError("Unrecognized captured field")

    def create(self, validated_data: dict[str, Any]) -> Any:
        return self.model._default_manager.create(**validated_data)

    def update(self, instance: Any, validated_data: dict[str, Any]) -> Any:
        for name, value in validated_data.items():
            setattr(instance, name, value)
        instance.save()
        return instance

    def save(self) -> Any:
        assert self._validated and not self.errors
        self.instance = (
            self.create(dict(self.validated_data))
            if self.instance is None
            else self.update(self.instance, dict(self.validated_data))
        )
        return self.instance

    @property
    def data(self) -> dict[str, Any]:
        result = {}
        for name, field in self.fields.items():
            value = getattr(self.instance, name)
            if value is not None:
                if field["kind"] == "nested":
                    value = [field["serializer"](item).data for item in value.all()]
                elif field["kind"] == "DecimalField":
                    value = str(value.quantize(Decimal(1).scaleb(-field["decimal_places"])))
            result[name] = value
        return result


def authenticate_basic(request: Any) -> None:
    import base64
    import binascii

    from django.contrib.auth import authenticate  # type: ignore[import-untyped]

    from .native_runtime import AuthenticationFailed

    parts = request.headers.get("Authorization", "").split()
    if not parts or parts[0].lower() != "basic":
        return
    if len(parts) != 2:
        raise AuthenticationFailed(
            "Invalid basic header. "
            + (
                "No credentials provided."
                if len(parts) == 1
                else "Credentials string should not contain spaces."
            )
        )
    try:
        raw = base64.b64decode(parts[1])
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = raw.decode("latin-1")
        username, password = decoded.split(":", 1)
    except (ValueError, binascii.Error):
        raise AuthenticationFailed(
            "Invalid basic header. Credentials not correctly base64 encoded."
        ) from None
    user = authenticate(username=username, password=password)
    if user is None:
        raise AuthenticationFailed("Invalid username/password.")
