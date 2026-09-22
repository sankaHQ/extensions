# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Exact wire values using the existing PostgreSQL codecs; never float money."""

from __future__ import annotations

import ast
from typing import Any

GO_VALUES = r"""// SPDX-License-Identifier: Apache-2.0
package backend

import (
    "encoding/json"
    "bytes"
    "strconv"
    "fmt"
    "regexp"
    "strings"
    "time"
    "github.com/jackc/pgx/v5/pgtype"
)

func validJSON(raw []byte) bool {
    decoder := json.NewDecoder(bytes.NewReader(raw))
    decoder.UseNumber()
    var value any
    if decoder.Decode(&value) != nil { return false }
    var valid func(any) bool
    valid = func(item any) bool {
        switch typed := item.(type) {
        case nil, bool, string: return true
        case json.Number:
            n, err := strconv.ParseInt(string(typed), 10, 64)
            return err == nil && n >= -9007199254740991 && n <= 9007199254740991
        case []any:
            for _, child := range typed { if !valid(child) { return false } }
            return true
        case map[string]any:
            for _, child := range typed { if !valid(child) { return false } }
            return true
        }
        return false
    }
    return valid(value)
}

type UUIDValue string
type DateValue string
type TimestampValue string
type DecimalValue string
type JSONValue = json.RawMessage

var canonicalUUID = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
var canonicalDecimal = regexp.MustCompile(`^-?(0|[1-9][0-9]*)(\.[0-9]+)?$`)

func validDecimal(value string, precision, scale int) bool {
    if !canonicalDecimal.MatchString(value) { return false }
    parts := strings.Split(strings.TrimPrefix(value, "-"), ".")
    fraction := ""
    if len(parts) == 2 { fraction = parts[1] }
    if len(fraction) != scale || len(strings.TrimLeft(parts[0], "0")) > precision-scale { return false }
    if strings.HasPrefix(value, "-") && strings.Trim(value[1:], "0.") == "" { return false }
    return true
}
func parseUUIDLookup(value string) (string, error) {
    if !canonicalUUID.MatchString(value) { return "", fmt.Errorf("invalid UUID") }
    return value, nil
}
func (v *UUIDValue) UnmarshalJSON(raw []byte) error {
    var value string
    if err := json.Unmarshal(raw, &value); err != nil { return err }
    if !canonicalUUID.MatchString(value) { return fmt.Errorf("invalid UUID") }
    *v = UUIDValue(value)
    return nil
}
func (v UUIDValue) UUIDValue() (pgtype.UUID, error) {
    var value pgtype.UUID
    err := value.Scan(string(v))
    return value, err
}
func (v *UUIDValue) ScanUUID(value pgtype.UUID) error {
    if !value.Valid { return fmt.Errorf("unexpected SQL NULL UUID") }
    raw, err := value.Value()
    if err != nil { return err }
    *v = UUIDValue(raw.(string))
    return nil
}
func (v *DateValue) UnmarshalJSON(raw []byte) error {
    var value string
    if err := json.Unmarshal(raw, &value); err != nil { return err }
    parsed, err := time.Parse("2006-01-02", value)
    if err != nil || parsed.Year() < 1 || parsed.Format("2006-01-02") != value { return fmt.Errorf("invalid date") }
    *v = DateValue(value)
    return nil
}
func (v DateValue) DateValue() (pgtype.Date, error) {
    parsed, err := time.Parse("2006-01-02", string(v))
    return pgtype.Date{Time: parsed, Valid: err == nil}, err
}
func (v *DateValue) ScanDate(value pgtype.Date) error {
    if !value.Valid || value.InfinityModifier != pgtype.Finite || value.Time.Year() < 1 || value.Time.Year() > 9999 { return fmt.Errorf("unsupported date") }
    *v = DateValue(value.Time.Format("2006-01-02"))
    return nil
}
const timestampLayout = "2006-01-02T15:04:05.000000+00:00"
func (v *TimestampValue) UnmarshalJSON(raw []byte) error {
    var value string
    if err := json.Unmarshal(raw, &value); err != nil { return err }
    parsed, err := time.Parse(timestampLayout, value)
    if err != nil || parsed.Year() < 1 || parsed.Format(timestampLayout) != value { return fmt.Errorf("invalid timestamp") }
    *v = TimestampValue(value)
    return nil
}
func (v TimestampValue) TimestamptzValue() (pgtype.Timestamptz, error) {
    parsed, err := time.Parse(timestampLayout, string(v))
    return pgtype.Timestamptz{Time: parsed, Valid: err == nil}, err
}
func (v *TimestampValue) ScanTimestamptz(value pgtype.Timestamptz) error {
    if !value.Valid || value.InfinityModifier != pgtype.Finite { return fmt.Errorf("unsupported timestamp") }
    utc := value.Time.UTC()
    if utc.Year() < 1 || utc.Year() > 9999 { return fmt.Errorf("unsupported timestamp") }
    *v = TimestampValue(utc.Format(timestampLayout))
    return nil
}
func (v *DecimalValue) UnmarshalJSON(raw []byte) error {
    var value string
    if err := json.Unmarshal(raw, &value); err != nil { return err }
    if !canonicalDecimal.MatchString(value) { return fmt.Errorf("invalid decimal") }
    *v = DecimalValue(value)
    return nil
}
func (v DecimalValue) NumericValue() (pgtype.Numeric, error) {
    var value pgtype.Numeric
    err := value.Scan(string(v))
    return value, err
}
func (v *DecimalValue) ScanNumeric(value pgtype.Numeric) error {
    if !value.Valid || value.NaN || value.InfinityModifier != pgtype.Finite { return fmt.Errorf("unsupported numeric") }
    raw, err := value.Value()
    if err != nil { return err }
    *v = DecimalValue(raw.(string))
    return nil
}
"""


GO_NATIVE_VALUES = r"""// SPDX-License-Identifier: Apache-2.0
package backend

import ("bytes"; "encoding/json"; "fmt"; "math"; "math/big"; "regexp"; "strconv"; "strings"; "unicode")

func decimalDigit(char rune) rune {
    if char < 128 { return char }
    for _, row := range unicode.Digit.R16 {
        if uint32(char) >= uint32(row.Lo) && uint32(char) <= uint32(row.Hi) && (uint32(char)-uint32(row.Lo))%uint32(row.Stride) == 0 {
            return rune('0' + ((uint32(char)-uint32(row.Lo))/uint32(row.Stride))%10)
        }
    }
    for _, row := range unicode.Digit.R32 {
        if uint32(char) >= row.Lo && uint32(char) <= row.Hi && (uint32(char)-row.Lo)%row.Stride == 0 {
            return rune('0' + ((uint32(char)-row.Lo)/row.Stride)%10)
        }
    }
    return char
}

// Native UUID parsers differ: Pydantic uses the Rust UUID formats, DRF
// delegates strings to Python UUID(hex=...) and accepts 128-bit integers.
func nativeUUID(raw []byte, drf bool) (UUIDValue, error) {
    invalid := fmt.Errorf("invalid UUID")
    var value string
    if err := json.Unmarshal(raw, &value); err != nil {
        if !drf { return "", invalid }
        text := string(bytes.TrimSpace(raw))
        if text == "true" { text = "1" }; if text == "false" { text = "0" }
        number, ok := new(big.Int).SetString(text, 10)
        if !ok || number.Sign() < 0 || number.BitLen() > 128 { return "", invalid }
        value = fmt.Sprintf("%032x", number)
    } else if drf {
        value = strings.ReplaceAll(strings.ReplaceAll(value, "urn:", ""), "uuid:", "")
        value = strings.ReplaceAll(strings.Trim(value, "{}"), "-", "")
        if len([]rune(value)) != 32 { return "", invalid }
        // Python int(hex, 16) permits surrounding whitespace, a plus sign,
        // an optional 0x prefix, Unicode decimal digits and digit separators.
        value = strings.TrimSpace(value)
        value = strings.TrimPrefix(value, "+")
        prefix := strings.HasPrefix(value, "0x") || strings.HasPrefix(value, "0X")
        if prefix { value = strings.TrimPrefix(value[2:], "_") }
        var digits strings.Builder
        separator := true
        for _, char := range value {
            if char == '_' {
                if separator { return "", invalid }; separator = true; continue
            }
            char = decimalDigit(char)
            if !(char >= '0' && char <= '9' || char >= 'a' && char <= 'f' || char >= 'A' && char <= 'F') { return "", invalid }
            digits.WriteRune(char); separator = false
        }
        if separator { return "", invalid }
        number, ok := new(big.Int).SetString(digits.String(), 16)
        if !ok || number.BitLen() > 128 { return "", invalid }
        value = fmt.Sprintf("%032x", number)
    } else {
        if len(value) == 45 && strings.HasPrefix(value, "urn:uuid:") { value = value[9:] }
        if len(value) == 38 && value[0] == '{' && value[37] == '}' { value = value[1:37] }
        if len(value) == 36 {
            for _, index := range []int{8, 13, 18, 23} { if value[index] != '-' { return "", invalid } }
            value = strings.ReplaceAll(value, "-", "")
        }
        if len(value) != 32 { return "", invalid }
    }
    value = strings.ToLower(value)
    if len(value) != 32 { return "", invalid }
    value = value[:8] + "-" + value[8:12] + "-" + value[12:16] + "-" + value[16:20] + "-" + value[20:]
    if !canonicalUUID.MatchString(value) { return "", invalid }
    return UUIDValue(value), nil
}
var nativeDecimalPattern = regexp.MustCompile(`^([+-]?)([0-9]*)(?:\.([0-9]*))?(?:[eE]([+-]?[0-9]+))?$`)

func roundDecimalDigits(digits string, keep int64) string {
    if keep < 0 { return "0" }
    if keep >= int64(len(digits)) { return digits }
    prefix := digits[:int(keep)]
    next := digits[int(keep)]
    up := next > '5' || next == '5' && (strings.Trim(digits[int(keep)+1:], "0") != "" || keep > 0 && (digits[int(keep)-1]-'0')%2 != 0)
    if prefix == "" { prefix = "0" }
    if up { number, _ := new(big.Int).SetString(prefix, 10); number.Add(number, big.NewInt(1)); prefix = number.String() }
    return prefix
}

func nativeDecimal(raw []byte, precision, scale int, drf bool) (DecimalValue, error) {
    invalid := fmt.Errorf("invalid decimal")
    var input any
    decoder := json.NewDecoder(bytes.NewReader(raw)); decoder.UseNumber()
    if err := decoder.Decode(&input); err != nil { return "", invalid }
    var text string
    switch value := input.(type) {
    case string:
        text = strings.TrimFunc(value, func(r rune) bool { return unicode.IsSpace(r) || r >= 0x1c && r <= 0x1f })
    case json.Number:
        text = string(value)
        if strings.ContainsAny(text, ".eE") {
            // Match Python's JSON float input before converting to Decimal.
            number, err := strconv.ParseFloat(text, 64)
            if err != nil || math.IsInf(number, 0) || math.IsNaN(number) { return "", invalid }
            format := byte('e')
            if number == 0 || math.Abs(number) >= 1e-4 && math.Abs(number) < 1e16 { format = 'f' }
            text = strconv.FormatFloat(number, format, -1, 64)
            if format == 'f' && !strings.Contains(text, ".") { text += ".0" }
        }
    default: return "", invalid
    }
    if drf && len([]rune(text)) > 1000 { return "", invalid }
    var normalized strings.Builder
    for _, char := range text {
        if char == '_' { continue }
        char = decimalDigit(char)
        normalized.WriteRune(char)
    }
    match := nativeDecimalPattern.FindStringSubmatch(normalized.String())
    if match == nil || match[2] + match[3] == "" { return "", invalid }
    exponent := int64(0)
    if match[4] != "" {
        var err error
        exponent, err = strconv.ParseInt(match[4], 10, 64)
        if err != nil || exponent > 999999999999999999 || exponent < -1999999999999999997 { return "", invalid }
    }
    exponent -= int64(len(match[3]))
    if exponent < -1999999999999999997 { return "", invalid }
    digits := strings.TrimLeft(match[2] + match[3], "0")
    if digits == "" { digits = "0" }
    originalDigits, originalExponent := digits, exponent
    originalPlaces := max(-exponent, 0)
    originalWhole := max(int64(len(digits))+exponent, 0)
    if !drf {
        // Pydantic validates Decimal.normalize() in Python's default 28-digit
        // half-even context, but PostgreSQL receives the original exact value.
        contextExponent := max(exponent + int64(len(digits)) - 28, -1000026)
        if exponent < contextExponent {
            digits = roundDecimalDigits(digits, int64(len(digits)) + exponent - contextExponent)
            exponent = contextExponent
        }
        if digits == "0" { exponent = 0 } else {
            stripped := strings.TrimRight(digits, "0")
            exponent += int64(len(digits)-len(stripped)); digits = stripped
        }
    }
    places := max(-exponent, 0)
    whole := max(int64(len(digits))+exponent, 0)
    total := whole + places
    if !drf {
        total = min(total, originalWhole+originalPlaces)
        places = min(places, originalPlaces)
        whole = min(whole, originalWhole)
    }
    if total > int64(precision) || places > int64(scale) || whole > int64(precision-scale) { return "", invalid }
    if !drf {
        sign := ""; if match[1] == "-" { sign = "-" }
        return DecimalValue(sign + originalDigits + "e" + strconv.FormatInt(originalExponent, 10)), nil
    }
    // DRF quantizes before persistence; validation bounds the output allocation.
    digits, exponent = originalDigits, originalExponent
    if digits == "0" { exponent = 0 }
    padding := exponent + int64(scale)
    if padding < 0 || padding > int64(precision) { return "", invalid }
    digits += strings.Repeat("0", int(padding))
    if len(digits) <= scale { digits = strings.Repeat("0", scale+1-len(digits)) + digits }
    if scale > 0 { digits = digits[:len(digits)-scale] + "." + digits[len(digits)-scale:] }
    digits = strings.TrimLeft(digits, "0")
    if digits == "" || digits[0] == '.' { digits = "0" + digits }
    if match[1] == "-" && (drf || strings.Trim(digits, "0.") != "") { digits = "-" + digits }
    return DecimalValue(digits), nil
}
"""


def render_values(models: list[dict[str, Any]], native: bool = False) -> dict[str, str]:
    if any(field["go_type"].endswith("Value") for model in models for field in model["fields"]):
        files = {"values.go": GO_VALUES}
        if native:
            files["native_values.go"] = GO_NATIVE_VALUES
        return files
    return {}


SOURCE_IMPORTS = {
    "uuid": {"UUID"},
    "datetime": {"date", "datetime", "timezone"},
    "decimal": {"Decimal"},
    "re": {"fullmatch"},
}
SOURCE_VALIDATORS = r"""
def valid_uuid(value):
    return type(value) is str and fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value) is not None

def valid_date(value):
    if type(value) is not str:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False

def valid_timestamp(value):
    if type(value) is not str:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.tzinfo is not None and parsed.astimezone(timezone.utc).isoformat(timespec="microseconds") == value
    except (ValueError, OverflowError):
        return False

def valid_decimal(value, precision, scale):
    if type(value) is not str or fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", value) is None:
        return False
    parts = value.lstrip("-").split(".")
    fraction = parts[1] if len(parts) == 2 else ""
    return len(fraction) == scale and len(parts[0].lstrip("0")) <= precision - scale and not (value.startswith("-") and not value[1:].strip("0."))

def valid_json(value):
    if value is None or type(value) in (bool, str):
        return True
    if type(value) is int:
        return -9007199254740991 <= value <= 9007199254740991
    if type(value) is list:
        return all(valid_json(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and valid_json(item) for key, item in value.items())
    return False
"""


def normalize_values(tree: ast.Module, rich_models: bool = False) -> set[str]:
    """Recognize pure validators without erasing shadowed global bindings."""
    expected = {
        node.name: ast.dump(node)
        for node in ast.parse(SOURCE_VALIDATORS).body
        if isinstance(node, ast.FunctionDef)
    }
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in expected:
            if node.name in found or ast.dump(node) != expected[node.name]:
                raise ValueError("rich-value validators must match the explicit pure contract")
            found.add(node.name)
    if not found and not rich_models:
        return found
    protected = (
        found
        | {name for names in SOURCE_IMPORTS.values() for name in names}
        | {
            "type",
            "str",
            "len",
            "all",
            "bool",
            "int",
            "list",
            "dict",
            "ValueError",
            "OverflowError",
            "format",
        }
    )
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in found:
            continue
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name in protected
        ):
            raise ValueError("rich-value symbol is shadowed")
        if any(isinstance(item, ast.arg) and item.arg in protected for item in ast.walk(node)):
            raise ValueError("rich-value symbol is shadowed by a parameter")
        if any(
            isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store) and item.id in protected
            for item in ast.walk(node)
        ):
            raise ValueError("rich-value symbol is shadowed")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name) in protected and not (
                    isinstance(node, ast.ImportFrom)
                    and not node.level
                    and not alias.asname
                    and alias.name in SOURCE_IMPORTS.get(node.module or "", set())
                ):
                    raise ValueError("rich-value symbol provenance differs")
    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and not node.level
        for alias in node.names
        if not alias.asname and alias.name in SOURCE_IMPORTS.get(node.module or "", set())
    }
    required = {
        "valid_uuid": {"fullmatch"},
        "valid_date": {"date"},
        "valid_timestamp": {"datetime", "timezone"},
        "valid_decimal": {"fullmatch"},
        "valid_json": set(),
    }
    if set().union(*(required[name] for name in found)) - imported:
        raise ValueError("rich-value validator imports are missing")
    tree.body = [
        node for node in tree.body if not (isinstance(node, ast.FunctionDef) and node.name in found)
    ]
    return found


def input_value(field: dict[str, Any], data: str, partial: bool = False) -> str:
    name = repr(field["name"])
    value = f"{data}[{name}]"
    if not partial:
        if "default" in field:
            value = f"{data}.get({name}, {field['default']!r})"
        elif field["nullable"]:
            value = f"{data}.get({name})"
    constructor = {
        "UUIDValue": "UUID",
        "DateValue": "date.fromisoformat",
        "TimestampValue": "datetime.fromisoformat",
        "DecimalValue": "Decimal",
    }.get(field["go_type"])
    if constructor and not field.get("native_input"):
        parsed = f"{constructor}({value})"
        return f"(None if {value} is None else {parsed})" if field["nullable"] else parsed
    return value


def output_value(field: dict[str, Any], value: str) -> str:
    kind = field["go_type"]
    expression = value
    if kind == "UUIDValue":
        expression = f"str({value})"
    elif kind == "DecimalValue":
        expression = f"format({value}, 'f')"
    elif kind == "DateValue":
        expression = f"{value}.isoformat()"
    elif kind == "TimestampValue":
        expression = f'{value}.astimezone(timezone.utc).isoformat(timespec="microseconds")'
    if expression != value and field["nullable"]:
        return f"(None if {value} is None else {expression})"
    return expression


def invalid_value(field: dict[str, Any], value: str) -> str:
    kind = field["go_type"]
    if field.get("native_input") and kind.endswith("Value"):
        # Synthetic validation is matched only after the native schema was qualified.
        return "False"
    validator = {
        "UUIDValue": "valid_uuid",
        "DateValue": "valid_date",
        "TimestampValue": "valid_timestamp",
        "JSONValue": "valid_json",
    }.get(kind)
    if validator:
        invalid = f"not {validator}({value})"
        if kind == "JSONValue" and not field["nullable"] and field["none_as_null"]:
            invalid = f"{value} is None or ({invalid})"
        return invalid
    if kind == "DecimalValue":
        precision, scale = field["sql_type"][8:-1].split(",")
        return f"not valid_decimal({value}, {precision}, {scale})"
    python_type = {"string": "str", "bool": "bool"}[kind]
    return f"type({value}) is not {python_type}"


def uuid_lookup_prefix(field: dict[str, Any], framework: str) -> str:
    if field["go_type"] != "UUIDValue":
        return ""
    failure = {
        "drf": 'return Response({"error": "invalid lookup"}, status=400)',
        "flask": 'return jsonify({"error": "invalid lookup"}), 400',
        "fastapi": 'raise HTTPException(status_code=400, detail="invalid lookup")',
    }[framework]
    name = field["name"]
    return f"if not valid_uuid({name}):\n    {failure}\n{name} = UUID({name})\n"
