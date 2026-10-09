# SPDX-License-Identifier: Apache-2.0
"""Real Go candidate replay; no provider calls or benchmark fixtures."""

import http.client
import json
import os
import shutil
import signal
import sys
from pathlib import Path

import pytest
from sanka_extension_python_to_golang.adapter import handle

from sanka_extensions.code import ExtensionRequest


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("SANKA_GO_REPLAY_TESTS") != "1", reason="requires cached Go toolchain"
)
@pytest.mark.parametrize(
    "mode",
    ["match", "status", "write", "missing", "syntax", "cache", "interrupted", "response-timeout"],
)
def test_go_candidate_response_parity(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "settings.py").write_text(
        "import os\nSECRET_KEY='offline-fixture'\nROOT_URLCONF='urls'\n"
        "ALLOWED_HOSTS=['testserver']\nINSTALLED_APPS=[]\n"
        "DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3',"
        "'NAME':os.environ['REPLAY_DB']}}\n"
    )
    (tmp_path / "urls.py").write_text(
        "from django.http import JsonResponse\nfrom django.urls import path\n"
        "def health(request):\n"
        "    status = 200 if request.method=='GET' else "
        "(400 if request.COOKIES.get('session')=='ok' else 403)\n"
        "    response=JsonResponse({'ready':True}, status=status)\n"
        "    response.set_cookie('session', 'ok')\n    return response\n"
        "urlpatterns=[path('health/', health)]\n"
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    locks = Path(__file__).parents[1] / "src/sanka_extension_python_to_golang/locks/fiber-sqlite"
    for name in ("go.mod", "go.sum"):
        shutil.copyfile(locks / name, candidate / name)
    (tmp_path / "seed.py").write_text(
        "from django.db import connection\n"
        "with connection.cursor() as cursor: cursor.execute('CREATE TABLE audit (id INTEGER)')\n"
    )
    status = 503 if mode == "status" else 200
    mutation = 'db.Exec("INSERT INTO audit VALUES (1)");' if mode == "write" else ""

    (candidate / "ready.csv").write_text('{"ready":true}')
    (candidate / "main.go").write_text(
        'package main\nimport ("net/http"; "os"; "database/sql"; '
        '_ "modernc.org/sqlite"; _ "embed")\n'
        "//go:embed ready.csv\nvar reply []byte\n"
        "func main() {\n"
        'db,err:=sql.Open("sqlite",os.Getenv("DATABASE_URL"));'
        "if err!=nil {panic(err)}; defer db.Close();"
        'http.HandleFunc("/health/", func(w http.ResponseWriter,r *http.Request) {'
        'w.Header().Set("Content-Type","application/json");'
        'w.Header().Add("Set-Cookie","session=ok; Path=/");'
        f'if r.Method=="POST" {{ {mutation} c,e:=r.Cookie("session"); '
        'if e!=nil || c.Value!="ok" {w.WriteHeader(403)} else {w.WriteHeader(400)} } '
        f"else {{ w.WriteHeader({status}) }}; "
        "w.Write(reply)"
        '}); http.ListenAndServe("127.0.0.1:"+os.Getenv("PORT"),nil) }\n'
    )
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {"id": "health", "method": "GET", "path": "/health/"},
                {
                    "id": "reject",
                    "method": "POST",
                    "path": "/health/",
                    "body": {},
                    "setup": [{"method": "GET", "path": "/health/"}],
                },
            ]
        )
    )
    artifacts = tmp_path / ".sanka/go"
    artifacts.mkdir(parents=True)
    (artifacts / "verify.json").write_text('{"ok": true}')
    if mode == "missing":
        (candidate / "main.go").unlink()
    elif mode == "syntax":
        (candidate / "main.go").write_text("not Go code")
    request = ExtensionRequest(
        "offline",
        "verify",
        str(tmp_path),
        str(artifacts),
        "sanka/python-to-golang",
        "0.1.0a23",
        "0" * 64,
        {},
        {
            "scenarios": "cases.json",
            "settings_module": "settings",
            "candidate": "candidate",
            "entrypoint": "main.go",
            "db_env": "REPLAY_DB",
            "seed": "seed.py",
        },
        (),
        None,
    )
    monkeypatch.setenv("SANKA_GO_SOURCE_PYTHON", sys.executable)
    monkeypatch.setattr(sys, "executable", "/missing-extension-python")
    if mode == "cache":
        monkeypatch.setenv("GOMODCACHE", str(tmp_path / "empty-cache"))
    if mode == "interrupted":

        def interrupt(*args: object, **kwargs: object) -> None:
            os.kill(os.getpid(), signal.SIGTERM)

        monkeypatch.setattr(http.client.HTTPConnection, "request", interrupt)
    if mode == "response-timeout":

        def timeout(*args: object, **kwargs: object) -> None:
            raise TimeoutError()

        monkeypatch.setattr(http.client.HTTPConnection, "getresponse", timeout)
    response = handle(request)
    if mode in {"missing", "syntax", "cache", "interrupted", "response-timeout"}:
        assert response.outcome == "error"
        assert response.error.code == "SANKA_EXTENSION_REPLAY_INVALID"
        assert response.error.details["failure_category"] == (
            "infrastructure_failure" if mode in {"cache", "interrupted"} else "candidate_failure"
        )
        assert not (artifacts / "verify.json").exists()
        return
    assert (response.outcome == "success") is (mode == "match"), response.error
    report = json.loads(Path(response.data["report_path"]).read_text())
    assert report["summary"]["status_mismatches"] == (1 if mode == "status" else 0)
    assert report["summary"]["database_mismatches"] == (1 if mode == "write" else 0)
    assert not list(candidate.glob("*.sqlite3"))
    if mode == "match":
        before = response.data["candidate_digest"]
        (candidate / "ready.csv").write_text('{"ready":false}')
        changed = handle(request)
        assert changed.outcome == "error"
        assert changed.data["candidate_digest"] != before
        assert not json.loads((artifacts / "verify.json").read_text())["ok"]
