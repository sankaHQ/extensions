# SPDX-License-Identifier: Apache-2.0
"""Claim-scoped migrations cannot serve anonymous or missing-principal rows."""

import json
import os
import subprocess

import pytest
from sanka_extension_python_to_golang.capture import capture
from sanka_extension_python_to_golang.render import render
from test_golang_drf_project import scoped_project


@pytest.mark.parametrize("authenticated", [False, True])
def test_claim_scope_requires_authenticated_view(tmp_path, authenticated):
    config = scoped_project(tmp_path)
    if not authenticated:
        views = tmp_path / "orders/views.py"
        before = views.read_text()
        marker = before.index("class NoteViewSet")
        views.write_text(
            before[:marker]
            + before[marker:]
            .replace("authentication_classes = [JWTAuthentication]", "authentication_classes = []")
            .replace("permission_classes = [IsAuthenticated]", "permission_classes = []")
        )
    captured = capture(tmp_path, config)
    assert bool(captured["gaps"]) is not authenticated
    if authenticated:
        assert render(captured)
    else:
        with pytest.raises(ValueError):
            render(captured)


@pytest.mark.skipif(os.getenv("SANKA_GO_TESTS") != "1", reason="requires Go toolchain")
@pytest.mark.parametrize("database", ["pgx", "sqlite"])
def test_generated_claim_scope_denies_empty_principal(tmp_path, database):
    config = scoped_project(tmp_path) | {"database_layer": database, "target_framework": "chi"}
    output = tmp_path / "generated"
    for name, content in render(capture(tmp_path, config)).items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    from sanka_extension_python_to_golang.jwt_security import REPLAY_JWT_ENV, replay_token

    token = replay_token(
        {
            "iss": REPLAY_JWT_ENV["AUTH_JWT_ISSUER"],
            "aud": REPLAY_JWT_ENV["AUTH_JWT_AUDIENCE"],
            "sub": "fixture-user",
            "tenant": "fixture-tenant",
            "role": "writer",
            "exp": 4102444800,
        }
    )
    (output / "scope_security_test.go").write_text(
        """package backend
import ("context"; "testing")
func TestMissingPrincipal(t *testing.T) {
    view := &drfSchema.Project.Views[len(drfSchema.Project.Views)-1]
    authentication := view.Authentication
    view.Authentication = ""
    for _, method := range []string{"GET", "POST", "PATCH", "DELETE"} {
        path := "/"+view.Path
        if method=="PATCH" || method=="DELETE" { path += "1/" }
        status,_ := drfRequest(context.Background(),nil,method,path,
            []byte(`{"content":"x"}`),"application/json","application/json","","")
        if status!=403 { t.Fatalf("%s returned %d",method,status) }
    }
    view.Authentication = authentication
    view.Scope["owner"] = "missing-claim"
    SET_ENV
    status,_ := drfRequest(context.Background(),nil,"GET","/"+view.Path,nil,
        "application/json","application/json",TOKEN,"")
    if status!=403 { t.Fatalf("missing claim returned %d",status) }
}
""".replace("TOKEN", json.dumps(token)).replace(
            "SET_ENV",
            "\n".join(
                f"t.Setenv({json.dumps(key)}, {json.dumps(value)})"
                for key, value in REPLAY_JWT_ENV.items()
            ),
        )
    )
    result = subprocess.run(
        ["go", "test", "-mod=readonly", "-p=2", "-run", "TestMissingPrincipal", "./..."],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=180,
        env=os.environ | {"GOMAXPROCS": "2"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
