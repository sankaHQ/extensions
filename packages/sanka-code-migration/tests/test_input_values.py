# SPDX-License-Identifier: Apache-2.0
"""Field-presence contract; framework coercion remains separately qualified."""

import dataclasses
import json
import os
import subprocess
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from sanka_code_migration.values import DecimalValue, InputValue


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


@pytest.mark.parametrize("value", [1.2, float("nan"), [], {}, (1,)])
def test_unqualified_types_fail(value: object) -> None:
    with pytest.raises(ValueError):
        InputValue.from_field({"field": value}, "field")


@pytest.mark.parametrize("text", [" false", "1.0", "[]", "{}", "NaN", '"\\ud800"'])
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
    inputs = [{}, *({"field": v} for v in [None, False, True, 0, -1, 2**63, "", "日本語"])]
    result = subprocess.run(
        ["go", "run", str(probe)],
        input=json.dumps(inputs, ensure_ascii=False, separators=(",", ":")),
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
