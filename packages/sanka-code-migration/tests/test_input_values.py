# SPDX-License-Identifier: Apache-2.0
"""Field-presence contract; framework coercion remains separately qualified."""

import dataclasses
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sanka_code_migration.values import DecimalValue, InputValue, TimestampValue


@pytest.mark.parametrize("value", [None, False, True, 0, -1, 2**63, "", "日本語"])
def test_present_scalar(value: object) -> None:
    field = InputValue.from_field({"field": value}, "field")
    assert field.present
    assert field.is_null is (value is None)
    assert InputValue(field.to_dict()["value_json"]) == field
    with pytest.raises(dataclasses.FrozenInstanceError):
        field.value_json = "null"


def test_absent_is_not_null_or_false_or_zero() -> None:
    absent = InputValue.from_field({}, "field")
    assert not absent.present and not absent.is_null
    fields = [absent, *(InputValue.from_field({"field": v}, "field") for v in [None, False, 0, ""])]
    assert len(set(fields)) == 5
    assert InputValue.from_field({"field": 2**63}, "field").value_json == "9223372036854775808"


@pytest.mark.parametrize(
    "value", [1.2, float("nan"), (1,), {1: "bad"}, [{"nested": 1.2}], {"nested": (1,)}]
)
def test_unqualified_types_fail(value: object) -> None:
    with pytest.raises(ValueError):
        InputValue.from_field({"field": value}, "field")


@pytest.mark.parametrize(
    "text", [" false", "1.0", '{"b":0,"a":1}', '{"a":1,"a":2}', "[NaN]", '{"x":"\\ud800"}']
)
def test_invalid_wire_values_fail(text: str) -> None:
    with pytest.raises(ValueError):
        InputValue(text)


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires qualified Go toolchain")
def test_native_go_presence(tmp_path: Path) -> None:
    env = os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"}
    version = subprocess.run(["go", "version"], env=env, capture_output=True, text=True, check=True)
    assert version.stdout.split()[2] == "go1.26.5"
    probe = tmp_path / "probe.go"
    probe.write_text(r"""package main
import ("encoding/json"; "os")
func main() {
    var inputs []map[string]json.RawMessage
    if err := json.NewDecoder(os.Stdin).Decode(&inputs); err != nil { panic(err) }
    outputs := make([]map[string]any, 0, len(inputs))
    for _, input := range inputs {
        var value any
        if raw, present := input["field"]; present { value = string(raw) }
        outputs = append(outputs, map[string]any{
            "schema": "sanka.input-value/v1", "value_json": value,
        })
    }
    if err := json.NewEncoder(os.Stdout).Encode(outputs); err != nil { panic(err) }
}
""")
    inputs = [
        {},
        *({"field": v} for v in [None, False, True, 0, -1, 2**63, "", "日本語"]),
        *(
            {"field": v}
            for v in [
                [],
                {},
                [None, False, 0, "", {}, []],
                {
                    "items": [{"id": 2**100, "name": "日本語<&>\u2028"}, {}],
                    "flags": {"enabled": False},
                },
            ]
        ),
    ]
    result = subprocess.run(
        ["go", "run", str(probe)],
        input=json.dumps(inputs, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    assert json.loads(result.stdout) == [
        InputValue.from_field(item, "field").to_dict() for item in inputs
    ]


@pytest.mark.parametrize(
    "text", ["0", "-0.00", "1.2300", "9223372036854775809.01", "1E+999999", "1E-999999"]
)
def test_exact_decimal_contract(text: str) -> None:
    value = Decimal(text)
    with localcontext() as context:
        context.prec = 2
        field = DecimalValue.from_decimal(value)
        assert field.to_decimal().as_tuple() == value.as_tuple()
        assert DecimalValue(field.coefficient, field.exponent) == field
    assert len(json.dumps(field.to_dict())) < 150


@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"), 1.1, "1.00"],
)
def test_non_decimal_or_non_finite_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        DecimalValue.from_decimal(value)


@pytest.mark.parametrize(
    "coefficient, exponent",
    [("01", 0), ("+1", 0), ("1.0", 0), ("\u0661", 0), ("", 0), (1, 0), ("1", True), ("1", 1.5)],
)
def test_invalid_decimal_wire_rejected(coefficient: object, exponent: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        DecimalValue(coefficient, exponent)


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires qualified Go toolchain")
def test_native_go_decimal_contract(tmp_path: Path) -> None:
    env = os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"}
    probe = tmp_path / "decimal.go"
    probe.write_text(r"""package main
import ("encoding/json"; "os"; "math/big")
type Decimal struct {
    Schema string `json:"schema"`
    Coefficient string `json:"coefficient"`
    Exponent int64 `json:"exponent"`
}
func main() {
    var inputs []Decimal
    if err := json.NewDecoder(os.Stdin).Decode(&inputs); err != nil { panic(err) }
    for _, input := range inputs {
        if _, ok := new(big.Int).SetString(input.Coefficient, 10); !ok {
            panic("invalid coefficient")
        }
    }
    if err := json.NewEncoder(os.Stdout).Encode(inputs); err != nil { panic(err) }
}
""")
    values = [
        DecimalValue.from_decimal(Decimal(v)).to_dict()
        for v in ["-0.00", "1.2300", "9223372036854775809.01", "1E+999999", "1E-999999"]
    ]
    result = subprocess.run(
        ["go", "run", str(probe)],
        input=json.dumps(values),
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(result.stdout) == values


@pytest.mark.parametrize("seconds", [0, 19800, -12600, 1, -1, 86399, -86399])
def test_timestamp_exact_offset(seconds: int) -> None:
    source = datetime(2024, 2, 29, 23, 59, 59, 123456, timezone(timedelta(seconds=seconds)))
    value = TimestampValue.from_datetime(source)
    restored = value.to_datetime()
    assert restored == source
    assert restored.isoformat() == source.isoformat()
    assert value.offset_seconds == seconds
    assert TimestampValue(value.utc, value.offset_seconds) == value


def test_timestamp_dst_fold_preserves_instant() -> None:
    zone = ZoneInfo("America/New_York")
    first = TimestampValue.from_datetime(datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=0))
    second = TimestampValue.from_datetime(datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=1))
    assert first.utc == "2024-11-03T05:30:00.000000Z"
    assert second.utc == "2024-11-03T06:30:00.000000Z"
    assert (first.offset_seconds, second.offset_seconds) == (-14400, -18000)


@pytest.mark.parametrize(
    "value",
    [
        datetime(2024, 1, 1),
        "2024-01-01",
        datetime(2024, 1, 1, tzinfo=timezone(timedelta(microseconds=1))),
    ],
)
def test_timestamp_ambiguous_or_unqualified_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        TimestampValue.from_datetime(value)


@pytest.mark.parametrize(
    "utc, offset",
    [
        ("2024-02-30T00:00:00.000000Z", 0),
        ("2024-01-01T00:00:60.000000Z", 0),
        ("2024-01-01T00:00:00Z", 0),
        ("2024-01-01T00:00:00.000000+00:00", 0),
        ("2024-01-01T00:00:00.000000Z", True),
        ("2024-01-01T00:00:00.000000Z", 86400),
    ],
)
def test_timestamp_invalid_wire_rejected(utc: str, offset: object) -> None:
    with pytest.raises(ValueError):
        TimestampValue(utc, offset)


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires qualified Go toolchain")
def test_native_go_timestamp_contract(tmp_path: Path) -> None:
    env = os.environ | {"GOTOOLCHAIN": "local", "GOWORK": "off", "GOMAXPROCS": "2"}
    probe = tmp_path / "timestamp.go"
    probe.write_text(r"""package main
import ("encoding/json"; "os"; "time")
type Timestamp struct {
    UTC string `json:"utc"`
    Offset int `json:"offset_seconds"`
}
func main() {
    var inputs []Timestamp
    if err := json.NewDecoder(os.Stdin).Decode(&inputs); err != nil { panic(err) }
    outputs := make([][]int, 0, len(inputs))
    for _, input := range inputs {
        instant, err := time.Parse(time.RFC3339Nano, input.UTC)
        if err != nil { panic(err) }
        local := instant.In(time.FixedZone("", input.Offset))
        outputs = append(outputs, []int{local.Year(), int(local.Month()), local.Day(),
            local.Hour(), local.Minute(), local.Second(), local.Nanosecond()/1000})
    }
    if err := json.NewEncoder(os.Stdout).Encode(outputs); err != nil { panic(err) }
}
""")
    sources = [
        datetime(2024, 2, 29, 23, 59, 59, 123456, timezone(timedelta(seconds=s)))
        for s in [0, 19800, -12600, 1, -1, 86399, -86399]
    ]
    sources.extend([datetime(1, 1, 1, tzinfo=UTC), datetime(9999, 12, 31, 23, 59, 59, 999999, UTC)])
    result = subprocess.run(
        ["go", "run", str(probe)],
        input=json.dumps([TimestampValue.from_datetime(v).to_dict() for v in sources]),
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(result.stdout) == [
        [v.year, v.month, v.day, v.hour, v.minute, v.second, v.microsecond] for v in sources
    ]


def test_nested_inputs_preserve_presence_and_snapshot() -> None:
    payload = {
        "field": {
            "items": [{"id": 2**100}, {}, {"id": None}],
            "settings": {"enabled": False, "count": 0, "label": ""},
        }
    }
    field = InputValue.from_field(payload, "field")
    decoded = json.loads(field.value_json)
    fields = [InputValue.from_field(item, "id") for item in decoded["items"]]
    assert fields == [InputValue(str(2**100)), InputValue(), InputValue("null")]
    assert (
        len({InputValue.from_field({"x": value}, "x") for value in (None, {}, [], False, 0, "")})
        == 6
    )
    assert (
        InputValue.from_field({"field": dict(reversed(list(payload["field"].items())))}, "field")
        == field
    )
    payload["field"]["items"].append({"id": 4})
    assert json.loads(field.value_json) == decoded
    assert InputValue("[0,1]") != InputValue("[1,0]")


def test_nested_input_limits_and_cycles() -> None:
    value: object = None
    for _ in range(64):
        value = [value]
    field = InputValue.from_field({"x": value}, "x")
    assert InputValue(field.value_json) == field
    with pytest.raises(ValueError, match="nesting"):
        InputValue.from_field({"x": [value]}, "x")
    for depth in (65, 2000):
        with pytest.raises(ValueError, match="nesting"):
            InputValue("[" * depth + "null" + "]" * depth)
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="nesting"):
        InputValue.from_field({"x": cycle}, "x")
