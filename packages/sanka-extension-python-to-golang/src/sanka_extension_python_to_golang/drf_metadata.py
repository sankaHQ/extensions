# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""SimpleMetadata for the statically qualified DRF field subset."""

from __future__ import annotations

import re
from typing import Any


def field_info(field: dict[str, Any], *, declared: bool) -> dict[str, Any]:
    readonly = field["read_only"]
    kind = field["kind"]
    field_type = (
        "choice"
        if field.get("choices")
        else "decimal"
        if kind == "DecimalField"
        else "boolean"
        if kind == "BooleanField"
        else "integer"
        if field["go_type"] in {"int32", "int64"}
        else "string"
    )
    name = field["source_name"].replace("_", " ")
    info: dict[str, Any] = {
        "type": field_type,
        "required": field["required"] and not readonly,
        "read_only": readonly,
        "label": "ID" if kind in {"AutoField", "BigAutoField"} else name[:1].upper() + name[1:],
    }
    if readonly:
        return info
    if field_type == "choice":
        info["choices"] = [
            {"value": value, "display_name": label} for value, label in field["choice_labels"]
        ]
    elif field_type == "integer":
        if declared:
            info["min_value"] = field["minimum"]
        else:
            bits = 64 if field["go_type"] == "int64" else 32
            info.update(
                min_value=field.get("minimum", -(2 ** (bits - 1))), max_value=2 ** (bits - 1) - 1
            )
    elif field_type == "decimal":
        precision, scale = map(int, re.findall(r"\d+", field["sql_type"]))
        info.update(max_digits=precision, decimal_places=scale)
    elif kind == "CharField":
        info["max_length"] = int(re.search(r"\d+", field["sql_type"]).group())  # type: ignore[union-attr]
    return info


def serializer_info(schema: dict[str, Any]) -> dict[str, Any]:
    info = {field["source_name"]: field["metadata"] for field in schema["fields"]}
    if nested := schema.get("nested"):
        name = nested["name"].replace("_", " ")
        info[nested["name"]] = {
            "type": "field",
            "required": True,
            "read_only": False,
            "label": name[:1].upper() + name[1:],
            "child": {
                "type": "nested object",
                "required": True,
                "read_only": False,
                "children": serializer_info(nested["serializer"]),
            },
        }
    return info


def view_name(name: str) -> str:
    name = name.removesuffix("View").removesuffix("ViewSet")
    return re.sub(r"(((?<=[a-z])[A-Z])|([A-Z](?![A-Z]|$)))", r" \1", name).strip()


GO_HTTP = r"""
type drfHTML string

// Django's default DEBUG=False resolver response, before any DRF view dispatch.
const drfNotFoundHTML drfHTML = `
<!doctype html>
<html lang="en">
<head>
  <title>Not Found</title>
</head>
<body>
  <h1>Not Found</h1><p>The requested resource was not found on this server.</p>
</body>
</html>
`

func drfRoute(path string) (*drfView,int64,bool,string) {
    for i:=range drfSchema.Project.Views {
        view:=&drfSchema.Project.Views[i]
        if path==view.Path { return view,0,false,"" }
        for _,action:=range view.Actions {
            if path==view.Path+action.Name+"/" { return view,0,false,action.Kind }
        }
        if strings.HasPrefix(path,view.Path) && strings.HasSuffix(path,"/") {
            part:=strings.TrimSuffix(strings.TrimPrefix(path,view.Path),"/")
            if part!="" && !strings.ContainsAny(part,"/.") {
                id,err:=strconv.ParseInt(part,10,64);if err!=nil { id=-1 }
                return view,id,true,""
            }
        }
    }
    return nil,0,false,""
}

func drfAllowed(path string) string {
    view,_,detail,_:=drfRoute(path);if view==nil { return "" }
    if detail { path=view.Path+":id/" }
    selected:=map[string]bool{}
    for _,route:=range drfSchema.Routes { if route.Path==path { selected[route.Method]=true } }
    if len(selected)==0 { return "" }
    selected["HEAD"]=selected["GET"];selected["OPTIONS"]=true
    methods:=[]string{}
    for _,method:=range []string{"GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"} {
        if selected[method] { methods=append(methods,method) }
    }
    return strings.Join(methods,", ")
}

func drfOptions(view *drfView, detail bool, action, allowed string, found bool) map[string]any {
    name:=view.Name+" List";if detail { name=view.Name+" Instance" };if action!="" { name="Summary" }
    result:=map[string]any{"name":name,"description":"","renders":drfSchema.Project.Renders,
        "parses":[]string{"application/json","application/x-www-form-urlencoded","multipart/form-data"}}
    actions:=map[string]any{}
    if strings.Contains(allowed,"POST") { actions["POST"]=view.Metadata }
    if found && strings.Contains(allowed,"PUT") { actions["PUT"]=view.Metadata }
    if len(actions)>0 { result["actions"]=actions }
    return result
}
"""
